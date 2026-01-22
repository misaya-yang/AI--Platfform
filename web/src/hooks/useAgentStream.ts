/**
 * useAgentStream - Comprehensive hook for AG-UI protocol streaming
 *
 * This hook manages the entire streaming state including:
 * - Text content accumulation
 * - Tool calls with results
 * - Timeline state (via useAgentTimeline)
 * - Artifacts collection
 * - Connection state and error handling
 *
 * Usage:
 *   const { state, connect, disconnect, sendMessage } = useAgentStream();
 *
 *   // Connect to stream
 *   await connect('/api/v1/stream', { message: 'Hello' });
 *
 *   // Access state
 *   const { content, toolCalls, timeline, artifacts, isStreaming } = state;
 *
 *   // Render
 *   <StreamOutput text={content} isStreaming={isStreaming} />
 *   <AgentTimeline state={timeline} />
 *   <ArtifactList artifacts={artifacts} />
 */

import { useCallback, useReducer, useRef, useState } from "react";
import { useAgentTimeline } from "./useAgentTimeline";
import type { AGUIEvent } from "@/lib/sse";
import type { TimelineState } from "@/components/agent/AgentTimeline";
import type { ArtifactData } from "@/components/agent/ArtifactCard";
import { cancelTask } from "@/api/assistant";
import type { ErrorSeverity, AgentLoopPhase } from "@/api/assistant";

// Re-export for convenience
export type { AGUIEvent };

// ============================================================================
// Types
// ============================================================================

export type StreamStatus = "idle" | "connecting" | "streaming" | "completed" | "error" | "cancelled";

/**
 * Phase progress state for 8-step AgentLoop visualization.
 */
export interface PhaseProgress {
  currentPhase: number;     // 1-8
  totalPhases: number;      // 8
  phaseName: string;        // e.g., "memory_loading"
  displayName: string;      // Chinese display name
  status: "pending" | "running" | "completed";
  durationMs?: number;
}

/**
 * Structured error from backend.
 */
export interface StreamError {
  code: string;
  message: string;
  severity: ErrorSeverity;
  recoverable: boolean;
  phase?: AgentLoopPhase;
  suggestion?: string;
  details?: Record<string, unknown>;
  timestamp: number;
}

export interface ToolCallState {
  id: string;
  name: string;
  arguments: string;
  argumentsJson?: Record<string, unknown>;
  result?: string;
  resultJson?: unknown;
  status: "pending" | "running" | "completed" | "error";
  error?: string;
  startTime?: number;
  endTime?: number;
}

export interface AgentStreamState {
  status: StreamStatus;
  content: string;
  currentMessageId: string | null;
  toolCalls: ToolCallState[];
  artifacts: ArtifactData[];
  timeline: TimelineState;
  error: string | null;
  // Phase 1 Optimization: New fields
  taskId: string | null;            // For cancellation tracking
  errors: StreamError[];            // Structured errors list
  phaseProgress: PhaseProgress | null;  // Current phase progress
  hasFatalError: boolean;           // True if fatal error occurred
  metadata: {
    requestId?: string;
    runId?: string;
    threadId?: string;
    startTime?: number;
    endTime?: number;
    firstTokenTime?: number;
    tokenCount?: number;
  };
}

type StreamAction =
  | { type: "CONNECT" }
  | { type: "DISCONNECT" }
  | { type: "SET_ERROR"; payload: string }
  | { type: "TEXT_MESSAGE_START"; payload: { messageId: string } }
  | { type: "TEXT_CONTENT"; payload: string }
  | { type: "TEXT_MESSAGE_END" }
  | { type: "TOOL_CALL_START"; payload: ToolCallState }
  | { type: "TOOL_CALL_ARGS"; payload: { id: string; args: string } }
  | { type: "TOOL_CALL_RESULT"; payload: { id: string; result: string; status?: string } }
  | { type: "TOOL_CALL_END"; payload: { id: string; error?: string } }
  | { type: "ARTIFACT_ADDED"; payload: ArtifactData }
  | { type: "RUN_STARTED"; payload: { requestId?: string; runId?: string; threadId?: string; taskId?: string } }
  | { type: "RUN_FINISHED" }
  | { type: "RESET" }
  // Phase 1 Optimization: New actions
  | { type: "PHASE_STARTED"; payload: PhaseProgress }
  | { type: "PHASE_COMPLETED"; payload: { phaseIndex: number; durationMs: number } }
  | { type: "STRUCTURED_ERROR"; payload: StreamError }
  | { type: "CANCELLED"; payload: { reason: string } };

// ============================================================================
// Initial State
// ============================================================================

const initialState: Omit<AgentStreamState, "timeline"> = {
  status: "idle",
  content: "",
  currentMessageId: null,
  toolCalls: [],
  artifacts: [],
  error: null,
  // Phase 1 Optimization: New fields
  taskId: null,
  errors: [],
  phaseProgress: null,
  hasFatalError: false,
  metadata: {},
};

// ============================================================================
// Reducer
// ============================================================================

function streamReducer(
  state: Omit<AgentStreamState, "timeline">,
  action: StreamAction
): Omit<AgentStreamState, "timeline"> {
  switch (action.type) {
    case "CONNECT":
      return {
        ...initialState,
        status: "connecting",
        metadata: { startTime: Date.now() },
      };

    case "DISCONNECT":
      return {
        ...state,
        status: state.status === "error" ? "error" : "completed",
        metadata: { ...state.metadata, endTime: Date.now() },
      };

    case "SET_ERROR":
      return {
        ...state,
        status: "error",
        error: action.payload,
        metadata: { ...state.metadata, endTime: Date.now() },
      };

    case "TEXT_MESSAGE_START":
      return {
        ...state,
        status: "streaming",
        currentMessageId: action.payload.messageId,
        metadata: {
          ...state.metadata,
          firstTokenTime: state.metadata.firstTokenTime || Date.now(),
        },
      };

    case "TEXT_CONTENT":
      return {
        ...state,
        content: state.content + action.payload,
        metadata: {
          ...state.metadata,
          tokenCount: (state.metadata.tokenCount || 0) + 1,
        },
      };

    case "TEXT_MESSAGE_END":
      return {
        ...state,
        currentMessageId: null,
      };

    case "TOOL_CALL_START":
      return {
        ...state,
        toolCalls: [...state.toolCalls, action.payload],
      };

    case "TOOL_CALL_ARGS":
      return {
        ...state,
        toolCalls: state.toolCalls.map((tc) =>
          tc.id === action.payload.id
            ? {
                ...tc,
                arguments: tc.arguments + action.payload.args,
                argumentsJson: tryParseJson(tc.arguments + action.payload.args) as Record<string, unknown> | undefined,
              }
            : tc
        ),
      };

    case "TOOL_CALL_RESULT":
      return {
        ...state,
        toolCalls: state.toolCalls.map((tc) =>
          tc.id === action.payload.id
            ? {
                ...tc,
                result: action.payload.result,
                resultJson: tryParseJson(action.payload.result),
                status: (action.payload.status as ToolCallState["status"]) || "completed",
              }
            : tc
        ),
      };

    case "TOOL_CALL_END":
      return {
        ...state,
        toolCalls: state.toolCalls.map((tc) =>
          tc.id === action.payload.id
            ? {
                ...tc,
                status: action.payload.error ? "error" : "completed",
                error: action.payload.error,
                endTime: Date.now(),
              }
            : tc
        ),
      };

    case "ARTIFACT_ADDED":
      // Avoid duplicates
      if (state.artifacts.some((a) => a.id === action.payload.id)) {
        return state;
      }
      return {
        ...state,
        artifacts: [...state.artifacts, action.payload],
      };

    case "RUN_STARTED":
      return {
        ...state,
        status: "streaming",
        taskId: action.payload.taskId || null,
        metadata: {
          ...state.metadata,
          requestId: action.payload.requestId,
          runId: action.payload.runId,
          threadId: action.payload.threadId,
        },
      };

    case "RUN_FINISHED":
      return {
        ...state,
        status: "completed",
        metadata: { ...state.metadata, endTime: Date.now() },
      };

    // Phase 1 Optimization: New actions
    case "PHASE_STARTED":
      return {
        ...state,
        phaseProgress: action.payload,
      };

    case "PHASE_COMPLETED":
      return {
        ...state,
        phaseProgress: state.phaseProgress
          ? {
              ...state.phaseProgress,
              status: "completed",
              durationMs: action.payload.durationMs,
            }
          : null,
      };

    case "STRUCTURED_ERROR":
      return {
        ...state,
        errors: [...state.errors, action.payload],
        hasFatalError: state.hasFatalError || action.payload.severity === "fatal",
      };

    case "CANCELLED":
      return {
        ...state,
        status: "cancelled",
        error: action.payload.reason,
        metadata: { ...state.metadata, endTime: Date.now() },
      };

    case "RESET":
      return initialState;

    default:
      return state;
  }
}

function tryParseJson(str: string): unknown {
  try {
    return JSON.parse(str);
  } catch {
    return undefined;
  }
}

// ============================================================================
// Hook
// ============================================================================

interface UseAgentStreamOptions {
  onEvent?: (event: AGUIEvent) => void;
  onError?: (error: Error) => void;
  onComplete?: () => void;
}

export function useAgentStream(options: UseAgentStreamOptions = {}) {
  const [state, dispatch] = useReducer(streamReducer, initialState);
  const { state: timelineState, processEvent: processTimelineEvent, reset: resetTimeline } =
    useAgentTimeline();

  const abortControllerRef = useRef<AbortController | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  const processEvent = useCallback(
    (event: AGUIEvent) => {
      // Forward to timeline processor
      processTimelineEvent(event);

      // Forward to external handler
      options.onEvent?.(event);

      // Process for local state
      const eventType = event.event;

      switch (eventType) {
        case "run_started":
          dispatch({
            type: "RUN_STARTED",
            payload: {
              requestId: event.request_id,
              runId: event.run_id as string,
              threadId: event.thread_id as string,
              taskId: event.task_id as string,  // For cancellation tracking
            },
          });
          break;

        case "run_finished":
          dispatch({ type: "RUN_FINISHED" });
          options.onComplete?.();
          break;

        case "run_error":
          dispatch({
            type: "SET_ERROR",
            payload: (event.metadata as Record<string, string>)?.error || "Run failed",
          });
          break;

        case "text_message_start":
          dispatch({
            type: "TEXT_MESSAGE_START",
            payload: { messageId: event.message_id as string },
          });
          break;

        case "text_message_content":
        case "text_delta":
          dispatch({ type: "TEXT_CONTENT", payload: event.content as string });
          break;

        case "text_message_end":
          dispatch({ type: "TEXT_MESSAGE_END" });
          break;

        case "tool_call_start":
          dispatch({
            type: "TOOL_CALL_START",
            payload: {
              id: event.tool_call_id as string,
              name: event.tool_name as string,
              arguments: "",
              status: "running",
              startTime: Date.now(),
            },
          });
          break;

        case "tool_call_args":
          dispatch({
            type: "TOOL_CALL_ARGS",
            payload: {
              id: event.tool_call_id as string,
              args: event.arguments as string,
            },
          });
          break;

        case "tool_call_result":
          dispatch({
            type: "TOOL_CALL_RESULT",
            payload: {
              id: event.tool_call_id as string,
              result: JSON.stringify(event.result),
              status: event.status as string,
            },
          });
          break;

        case "tool_call_end":
          dispatch({
            type: "TOOL_CALL_END",
            payload: {
              id: event.tool_call_id as string,
              error: event.error as string,
            },
          });
          break;

        case "artifact_created":
        case "file_created":
          dispatch({
            type: "ARTIFACT_ADDED",
            payload: {
              id: (event.artifact_id || event.file_id) as string,
              type: (event.artifact_type as ArtifactData["type"]) || "file",
              name: (event.name || event.file_name) as string,
              url: event.url as string,
              mimeType: event.mime_type as string,
              size: event.size as number,
              status: "ready",
            },
          });
          break;

        case "document_generation_result":
          dispatch({
            type: "ARTIFACT_ADDED",
            payload: {
              id: (event.file_id || `doc-${Date.now()}`) as string,
              type: "document",
              name: event.title as string,
              url: event.url as string,
              format: event.doc_type as string,
              status: "ready",
            },
          });
          break;

        case "image_generation_result":
          dispatch({
            type: "ARTIFACT_ADDED",
            payload: {
              id: `img-${Date.now()}`,
              type: "image",
              name: ((event.prompt as string) || "Image").slice(0, 30),
              url: event.url as string,
              status: "ready",
            },
          });
          break;

        case "stream_end":
          dispatch({ type: "DISCONNECT" });
          break;

        // Phase 1 Optimization: Phase tracking events
        case "phase_started":
          dispatch({
            type: "PHASE_STARTED",
            payload: {
              currentPhase: event.phase_index as number,
              totalPhases: event.total_phases as number,
              phaseName: event.phase_name as string,
              displayName: event.display_name as string,
              status: "running",
            },
          });
          break;

        case "phase_completed":
          dispatch({
            type: "PHASE_COMPLETED",
            payload: {
              phaseIndex: event.phase_index as number,
              durationMs: event.duration_ms as number,
            },
          });
          break;

        // Phase 1 Optimization: Structured error events
        case "error":
          dispatch({
            type: "STRUCTURED_ERROR",
            payload: {
              code: event.code as string,
              message: event.message as string,
              severity: event.severity as ErrorSeverity,
              recoverable: event.recoverable as boolean,
              phase: event.phase as AgentLoopPhase | undefined,
              suggestion: event.suggestion as string | undefined,
              details: event.details as Record<string, unknown> | undefined,
              timestamp: Date.now(),
            },
          });
          break;

        // Phase 1 Optimization: Cancellation event
        case "cancelled":
          dispatch({
            type: "CANCELLED",
            payload: {
              reason: (event.reason as string) || "User requested cancellation",
            },
          });
          break;

        default:
          // Ignore other events
          break;
      }
    },
    [processTimelineEvent, options]
  );

  const connect = useCallback(
    async (url: string, body: Record<string, unknown>) => {
      // Abort any existing connection
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      const abortController = new AbortController();
      abortControllerRef.current = abortController;

      dispatch({ type: "CONNECT" });
      resetTimeline();
      setIsConnected(true);

      try {
        const response = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
          },
          body: JSON.stringify(body),
          signal: abortController.signal,
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        if (!response.body) {
          throw new Error("Response body is null");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();

          if (done) {
            dispatch({ type: "DISCONNECT" });
            break;
          }

          buffer += decoder.decode(value, { stream: true });

          // Parse SSE events
          const lines = buffer.split("\n");
          buffer = lines.pop() || ""; // Keep incomplete line in buffer

          let currentEventType = "";
          let currentData = "";

          for (const line of lines) {
            if (line.startsWith("event:")) {
              currentEventType = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              currentData = line.slice(5).trim();

              if (currentData) {
                try {
                  const event: AGUIEvent = JSON.parse(currentData);
                  // Use event type from SSE event field if available
                  if (currentEventType && !event.event) {
                    event.event = currentEventType as AGUIEvent["event"];
                  }
                  processEvent(event);
                } catch (e) {
                  console.warn("Failed to parse SSE event:", currentData, e);
                }
              }

              currentEventType = "";
              currentData = "";
            }
          }
        }
      } catch (error) {
        if ((error as Error).name === "AbortError") {
          // Intentional abort, not an error
          dispatch({ type: "DISCONNECT" });
        } else {
          const errorMessage = error instanceof Error ? error.message : "Stream error";
          dispatch({ type: "SET_ERROR", payload: errorMessage });
          options.onError?.(error instanceof Error ? error : new Error(errorMessage));
        }
      } finally {
        setIsConnected(false);
        abortControllerRef.current = null;
      }
    },
    [processEvent, resetTimeline, options]
  );

  const disconnect = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsConnected(false);
  }, []);

  const reset = useCallback(() => {
    disconnect();
    dispatch({ type: "RESET" });
    resetTimeline();
  }, [disconnect, resetTimeline]);

  /**
   * Cancel the current running task.
   * Calls the backend cancel API and disconnects the stream.
   */
  const cancel = useCallback(async (reason?: string) => {
    const taskId = state.taskId;
    if (!taskId) {
      console.warn("Cannot cancel: no taskId available");
      return false;
    }

    try {
      const result = await cancelTask(taskId, reason);
      if (result.cancelled) {
        // Disconnect the stream
        disconnect();
        dispatch({
          type: "CANCELLED",
          payload: { reason: reason || "User requested cancellation" },
        });
      }
      return result.cancelled;
    } catch (error) {
      console.error("Failed to cancel task:", error);
      // Still disconnect the stream even if API fails
      disconnect();
      // Dispatch CANCELLED to keep UI state consistent
      dispatch({
        type: "CANCELLED",
        payload: { reason: "Cancellation failed - stream closed" },
      });
      return false;
    }
  }, [state.taskId, disconnect]);

  // Combine state with timeline
  const fullState: AgentStreamState = {
    ...state,
    timeline: timelineState,
  };

  return {
    state: fullState,
    isStreaming: state.status === "streaming" || state.status === "connecting",
    isCancellable: state.taskId !== null && (state.status === "streaming" || state.status === "connecting"),
    isConnected,
    connect,
    disconnect,
    cancel,  // Phase 1 Optimization: Cancel function
    reset,
    processEvent, // Expose for manual event processing
  };
}

export default useAgentStream;
