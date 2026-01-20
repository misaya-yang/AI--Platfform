/**
 * useAgentTimeline - Hook for managing agent timeline state from AG-UI events
 *
 * This hook parses AG-UI protocol SSE events and maintains a real-time
 * timeline state that can be rendered by the AgentTimeline component.
 *
 * Usage:
 *   const { state, processEvent, reset } = useAgentTimeline();
 *
 *   // Process incoming SSE events
 *   eventSource.onmessage = (event) => {
 *     processEvent(JSON.parse(event.data));
 *   };
 *
 *   // Render timeline
 *   <AgentTimeline state={state} />
 */

import { useCallback, useReducer } from "react";
import type {
  TimelineState,
  TimelineStep,
  TimelineArtifact,
  ToolCallInfo,
  StepType,
  StepStatus,
} from "@/components/agent/AgentTimeline";

// ============================================================================
// Types
// ============================================================================

/** AG-UI Event Types */
export type AGUIEventType =
  // Lifecycle
  | "run_started"
  | "run_finished"
  | "run_error"
  | "step_started"
  | "step_finished"
  // Text Message
  | "text_message_start"
  | "text_message_content"
  | "text_message_end"
  | "text_delta"
  // Tool Call
  | "tool_call_start"
  | "tool_call_args"
  | "tool_call_result"
  | "tool_call_end"
  | "tool_error"
  // State
  | "state_snapshot"
  | "state_delta"
  | "messages_snapshot"
  // Artifacts
  | "artifact_created"
  | "file_creating"
  | "file_created"
  // Document
  | "outline_ready"
  | "document_generation_start"
  | "document_generation_result"
  // Search
  | "search_started"
  | "search_progress"
  | "search_completed"
  // Code Execution
  | "code_execution_start"
  | "code_execution_result"
  // Image
  | "image_generation_start"
  | "image_generation_result"
  // Status
  | "status"
  // Special
  | "stream_end"
  | "custom_event"
  | "raw_event";

/** Base AG-UI Event */
export interface AGUIEvent {
  event: AGUIEventType;
  request_id: string;
  chunk_index: number;
  timestamp: number;
  [key: string]: unknown;
}

/** Timeline reducer action types */
type TimelineAction =
  | { type: "RUN_STARTED"; payload: AGUIEvent }
  | { type: "RUN_FINISHED"; payload: AGUIEvent }
  | { type: "RUN_ERROR"; payload: AGUIEvent }
  | { type: "STEP_STARTED"; payload: AGUIEvent }
  | { type: "STEP_FINISHED"; payload: AGUIEvent }
  | { type: "TOOL_CALL_START"; payload: AGUIEvent }
  | { type: "TOOL_CALL_RESULT"; payload: AGUIEvent }
  | { type: "TOOL_CALL_END"; payload: AGUIEvent }
  | { type: "ARTIFACT_CREATED"; payload: AGUIEvent }
  | { type: "FILE_CREATED"; payload: AGUIEvent }
  | { type: "STATUS_UPDATE"; payload: AGUIEvent }
  | { type: "SEARCH_STARTED"; payload: AGUIEvent }
  | { type: "SEARCH_COMPLETED"; payload: AGUIEvent }
  | { type: "CODE_EXECUTION_START"; payload: AGUIEvent }
  | { type: "CODE_EXECUTION_RESULT"; payload: AGUIEvent }
  | { type: "IMAGE_GENERATION_START"; payload: AGUIEvent }
  | { type: "IMAGE_GENERATION_RESULT"; payload: AGUIEvent }
  | { type: "DOCUMENT_GENERATION_START"; payload: AGUIEvent }
  | { type: "DOCUMENT_GENERATION_RESULT"; payload: AGUIEvent }
  | { type: "OUTLINE_READY"; payload: AGUIEvent }
  | { type: "RESET" };

// ============================================================================
// Initial State
// ============================================================================

const initialState: TimelineState = {
  runId: "",
  status: "idle",
  steps: [],
  currentStepId: undefined,
  startTime: undefined,
  endTime: undefined,
  metadata: undefined,
};

// ============================================================================
// Helper Functions
// ============================================================================

function mapStepType(type: string): StepType {
  const typeMap: Record<string, StepType> = {
    planning: "planning",
    analysis: "analysis",
    outline: "outline",
    generating: "generating",
    validating: "validating",
    repairing: "repairing",
    tool_call: "tool_call",
    search: "search",
    code_execution: "code_execution",
    image_generation: "image_generation",
    document_generation: "document_generation",
    complete: "complete",
  };
  return typeMap[type] || "planning";
}

function updateStepStatus(
  steps: TimelineStep[],
  stepId: string,
  updates: Partial<TimelineStep>
): TimelineStep[] {
  return steps.map((step) => {
    if (step.id === stepId) {
      return { ...step, ...updates };
    }
    if (step.children) {
      return {
        ...step,
        children: updateStepStatus(step.children, stepId, updates),
      };
    }
    return step;
  });
}

function addToolCallToStep(
  steps: TimelineStep[],
  stepId: string,
  toolCall: ToolCallInfo
): TimelineStep[] {
  return steps.map((step) => {
    if (step.id === stepId) {
      return {
        ...step,
        toolCalls: [...(step.toolCalls || []), toolCall],
      };
    }
    if (step.children) {
      return {
        ...step,
        children: addToolCallToStep(step.children, stepId, toolCall),
      };
    }
    return step;
  });
}

function updateToolCallInStep(
  steps: TimelineStep[],
  stepId: string,
  toolCallId: string,
  updates: Partial<ToolCallInfo>
): TimelineStep[] {
  return steps.map((step) => {
    if (step.id === stepId && step.toolCalls) {
      return {
        ...step,
        toolCalls: step.toolCalls.map((tc) =>
          tc.id === toolCallId ? { ...tc, ...updates } : tc
        ),
      };
    }
    if (step.children) {
      return {
        ...step,
        children: updateToolCallInStep(step.children, stepId, toolCallId, updates),
      };
    }
    return step;
  });
}

function addArtifactToStep(
  steps: TimelineStep[],
  stepId: string,
  artifact: TimelineArtifact
): TimelineStep[] {
  return steps.map((step) => {
    if (step.id === stepId) {
      return {
        ...step,
        artifacts: [...(step.artifacts || []), artifact],
      };
    }
    if (step.children) {
      return {
        ...step,
        children: addArtifactToStep(step.children, stepId, artifact),
      };
    }
    return step;
  });
}

// ============================================================================
// Reducer
// ============================================================================

function timelineReducer(
  state: TimelineState,
  action: TimelineAction
): TimelineState {
  switch (action.type) {
    case "RUN_STARTED": {
      const { run_id, thread_id, metadata, timestamp } = action.payload;
      return {
        ...initialState,
        runId: run_id as string,
        status: "running",
        startTime: timestamp * 1000,
        metadata: {
          ...(metadata as Record<string, unknown>),
          threadId: thread_id,
        },
      };
    }

    case "RUN_FINISHED": {
      const { timestamp, metadata } = action.payload;
      return {
        ...state,
        status: "completed",
        endTime: timestamp * 1000,
        currentStepId: undefined,
        metadata: {
          ...state.metadata,
          ...(metadata as Record<string, unknown>),
        },
      };
    }

    case "RUN_ERROR": {
      const { timestamp, metadata } = action.payload;
      return {
        ...state,
        status: "error",
        endTime: timestamp * 1000,
        currentStepId: undefined,
        metadata: {
          ...state.metadata,
          ...(metadata as Record<string, unknown>),
        },
      };
    }

    case "STEP_STARTED": {
      const { step_id, step_name, step_type, parent_step_id, metadata, timestamp } =
        action.payload;
      const newStep: TimelineStep = {
        id: step_id as string,
        type: mapStepType(step_type as string),
        name: step_name as string,
        status: "running",
        startTime: timestamp * 1000,
        metadata: metadata as Record<string, unknown>,
      };

      // If there's a parent step, add as child
      if (parent_step_id) {
        return {
          ...state,
          currentStepId: step_id as string,
          steps: state.steps.map((step) => {
            if (step.id === parent_step_id) {
              return {
                ...step,
                children: [...(step.children || []), newStep],
              };
            }
            return step;
          }),
        };
      }

      return {
        ...state,
        currentStepId: step_id as string,
        steps: [...state.steps, newStep],
      };
    }

    case "STEP_FINISHED": {
      const { step_id, timestamp, metadata } = action.payload;
      const stepId = (step_id as string) || state.currentStepId;
      if (!stepId) return state;

      return {
        ...state,
        steps: updateStepStatus(state.steps, stepId, {
          status: "completed",
          endTime: timestamp * 1000,
          metadata: {
            ...(state.steps.find((s) => s.id === stepId)?.metadata || {}),
            ...(metadata as Record<string, unknown>),
          },
        }),
      };
    }

    case "TOOL_CALL_START": {
      const { tool_call_id, tool_name, timestamp } = action.payload;
      const toolCall: ToolCallInfo = {
        id: tool_call_id as string,
        name: tool_name as string,
        status: "running",
      };

      // Add to current step
      const stepId = state.currentStepId;
      if (!stepId) {
        // Create a tool call step
        const newStep: TimelineStep = {
          id: `step-tc-${tool_call_id}`,
          type: "tool_call",
          name: tool_name as string,
          status: "running",
          startTime: timestamp * 1000,
          toolCalls: [toolCall],
        };
        return {
          ...state,
          currentStepId: newStep.id,
          steps: [...state.steps, newStep],
        };
      }

      return {
        ...state,
        steps: addToolCallToStep(state.steps, stepId, toolCall),
      };
    }

    case "TOOL_CALL_RESULT":
    case "TOOL_CALL_END": {
      const { tool_call_id, tool_name, result, error, status, timestamp } = action.payload;
      const stepId = state.currentStepId || `step-tc-${tool_call_id}`;

      return {
        ...state,
        steps: updateToolCallInStep(state.steps, stepId, tool_call_id as string, {
          result: result ? JSON.stringify(result) : undefined,
          status: (error ? "error" : (status as ToolCallInfo["status"]) || "completed"),
          durationMs: undefined, // Would need start time tracking
        }),
      };
    }

    case "ARTIFACT_CREATED":
    case "FILE_CREATED": {
      const { artifact_id, artifact_type, name, file_name, file_id, url, mime_type, size } =
        action.payload;
      const artifact: TimelineArtifact = {
        id: (artifact_id || file_id) as string,
        type: (artifact_type as TimelineArtifact["type"]) || "file",
        name: (name || file_name) as string,
        url: url as string,
        mimeType: mime_type as string,
        size: size as number,
      };

      const stepId = state.currentStepId;
      if (!stepId) {
        return state;
      }

      return {
        ...state,
        steps: addArtifactToStep(state.steps, stepId, artifact),
      };
    }

    case "STATUS_UPDATE": {
      const { status, message, phase, progress } = action.payload;
      const stepId = state.currentStepId;
      if (!stepId) return state;

      return {
        ...state,
        steps: updateStepStatus(state.steps, stepId, {
          message: message as string,
          progress: progress as number,
          metadata: {
            ...(state.steps.find((s) => s.id === stepId)?.metadata || {}),
            phase,
            status,
          },
        }),
      };
    }

    case "SEARCH_STARTED": {
      const { query, search_type, timestamp } = action.payload;
      const newStep: TimelineStep = {
        id: `search-${Date.now()}`,
        type: "search",
        name: `搜索: ${(query as string).slice(0, 30)}...`,
        status: "running",
        startTime: timestamp * 1000,
        message: query as string,
        metadata: { searchType: search_type },
      };
      return {
        ...state,
        currentStepId: newStep.id,
        steps: [...state.steps, newStep],
      };
    }

    case "SEARCH_COMPLETED": {
      const { query, result_count, timestamp } = action.payload;
      const stepId = state.currentStepId;
      if (!stepId) return state;

      return {
        ...state,
        steps: updateStepStatus(state.steps, stepId, {
          status: "completed",
          endTime: timestamp * 1000,
          message: `找到 ${result_count} 条结果`,
        }),
      };
    }

    case "CODE_EXECUTION_START": {
      const { code, language, timestamp } = action.payload;
      const newStep: TimelineStep = {
        id: `code-${Date.now()}`,
        type: "code_execution",
        name: `执行 ${language || "Python"} 代码`,
        status: "running",
        startTime: timestamp * 1000,
        metadata: { language },
      };
      return {
        ...state,
        currentStepId: newStep.id,
        steps: [...state.steps, newStep],
      };
    }

    case "CODE_EXECUTION_RESULT": {
      const { output, success, error, timestamp } = action.payload;
      const stepId = state.currentStepId;
      if (!stepId) return state;

      return {
        ...state,
        steps: updateStepStatus(state.steps, stepId, {
          status: success ? "completed" : "error",
          endTime: timestamp * 1000,
          message: error as string || (output as string)?.slice(0, 100),
        }),
      };
    }

    case "IMAGE_GENERATION_START": {
      const { prompt, timestamp } = action.payload;
      const newStep: TimelineStep = {
        id: `img-${Date.now()}`,
        type: "image_generation",
        name: "生成图片",
        status: "running",
        startTime: timestamp * 1000,
        message: (prompt as string)?.slice(0, 50),
      };
      return {
        ...state,
        currentStepId: newStep.id,
        steps: [...state.steps, newStep],
      };
    }

    case "IMAGE_GENERATION_RESULT": {
      const { url, prompt, timestamp } = action.payload;
      const stepId = state.currentStepId;
      if (!stepId) return state;

      const artifact: TimelineArtifact = {
        id: `img-artifact-${Date.now()}`,
        type: "image",
        name: (prompt as string)?.slice(0, 20) || "Generated Image",
        url: url as string,
      };

      return {
        ...state,
        steps: addArtifactToStep(
          updateStepStatus(state.steps, stepId, {
            status: "completed",
            endTime: timestamp * 1000,
          }),
          stepId,
          artifact
        ),
      };
    }

    case "DOCUMENT_GENERATION_START": {
      const { doc_type, title, timestamp } = action.payload;
      const newStep: TimelineStep = {
        id: `doc-${Date.now()}`,
        type: "document_generation",
        name: `生成 ${(doc_type as string)?.toUpperCase()} 文档`,
        status: "running",
        startTime: timestamp * 1000,
        message: title as string,
      };
      return {
        ...state,
        currentStepId: newStep.id,
        steps: [...state.steps, newStep],
      };
    }

    case "DOCUMENT_GENERATION_RESULT": {
      const { doc_type, title, url, file_id, timestamp } = action.payload;
      const stepId = state.currentStepId;
      if (!stepId) return state;

      const artifact: TimelineArtifact = {
        id: (file_id as string) || `doc-artifact-${Date.now()}`,
        type: "document",
        name: (title as string) || "Document",
        url: url as string,
        mimeType: (doc_type as string) === "docx"
          ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          : undefined,
      };

      return {
        ...state,
        steps: addArtifactToStep(
          updateStepStatus(state.steps, stepId, {
            status: "completed",
            endTime: timestamp * 1000,
          }),
          stepId,
          artifact
        ),
      };
    }

    case "OUTLINE_READY": {
      const { title, sections, timestamp } = action.payload;
      const stepId = state.currentStepId;
      if (!stepId) return state;

      return {
        ...state,
        steps: updateStepStatus(state.steps, stepId, {
          message: `大纲: ${title} (${(sections as string[])?.length} 章节)`,
          metadata: {
            ...(state.steps.find((s) => s.id === stepId)?.metadata || {}),
            outline: { title, sections },
          },
        }),
      };
    }

    case "RESET":
      return initialState;

    default:
      return state;
  }
}

// ============================================================================
// Hook
// ============================================================================

export function useAgentTimeline() {
  const [state, dispatch] = useReducer(timelineReducer, initialState);

  const processEvent = useCallback((event: AGUIEvent) => {
    const eventType = event.event;

    switch (eventType) {
      case "run_started":
        dispatch({ type: "RUN_STARTED", payload: event });
        break;
      case "run_finished":
        dispatch({ type: "RUN_FINISHED", payload: event });
        break;
      case "run_error":
        dispatch({ type: "RUN_ERROR", payload: event });
        break;
      case "step_started":
        dispatch({ type: "STEP_STARTED", payload: event });
        break;
      case "step_finished":
        dispatch({ type: "STEP_FINISHED", payload: event });
        break;
      case "tool_call_start":
        dispatch({ type: "TOOL_CALL_START", payload: event });
        break;
      case "tool_call_result":
        dispatch({ type: "TOOL_CALL_RESULT", payload: event });
        break;
      case "tool_call_end":
        dispatch({ type: "TOOL_CALL_END", payload: event });
        break;
      case "artifact_created":
        dispatch({ type: "ARTIFACT_CREATED", payload: event });
        break;
      case "file_created":
        dispatch({ type: "FILE_CREATED", payload: event });
        break;
      case "status":
        dispatch({ type: "STATUS_UPDATE", payload: event });
        break;
      case "search_started":
        dispatch({ type: "SEARCH_STARTED", payload: event });
        break;
      case "search_completed":
        dispatch({ type: "SEARCH_COMPLETED", payload: event });
        break;
      case "code_execution_start":
        dispatch({ type: "CODE_EXECUTION_START", payload: event });
        break;
      case "code_execution_result":
        dispatch({ type: "CODE_EXECUTION_RESULT", payload: event });
        break;
      case "image_generation_start":
        dispatch({ type: "IMAGE_GENERATION_START", payload: event });
        break;
      case "image_generation_result":
        dispatch({ type: "IMAGE_GENERATION_RESULT", payload: event });
        break;
      case "document_generation_start":
        dispatch({ type: "DOCUMENT_GENERATION_START", payload: event });
        break;
      case "document_generation_result":
        dispatch({ type: "DOCUMENT_GENERATION_RESULT", payload: event });
        break;
      case "outline_ready":
        dispatch({ type: "OUTLINE_READY", payload: event });
        break;
      // Ignore text events - they don't affect timeline
      case "text_message_start":
      case "text_message_content":
      case "text_message_end":
      case "text_delta":
      case "stream_end":
      case "state_snapshot":
      case "state_delta":
      case "messages_snapshot":
      case "custom_event":
      case "raw_event":
        // These don't affect the timeline state
        break;
      default:
        console.debug(`Unhandled AG-UI event: ${eventType}`);
    }
  }, []);

  const reset = useCallback(() => {
    dispatch({ type: "RESET" });
  }, []);

  return {
    state,
    processEvent,
    reset,
  };
}

export default useAgentTimeline;
