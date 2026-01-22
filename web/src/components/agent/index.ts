/**
 * Agent Components - Manus-style agent visualization
 *
 * This module provides components for visualizing agent execution,
 * including timeline, artifacts, status displays, and result cards.
 *
 * Phase 1 Frontend Style Guide Components:
 * - AgentStatusStream: Real-time thinking/execution flow
 * - CitationDrawer: RAG citation drawer with inline badges
 * - TaskResultCard: Structured output cards (code, image, summary, table)
 * - ErrorDisplay: Severity-based error display
 */

export {
  AgentTimeline,
  type TimelineState,
  type TimelineStep,
  type TimelineArtifact,
  type ToolCallInfo,
  type StepType,
  type StepStatus,
} from "./AgentTimeline";

export {
  ArtifactCard,
  ArtifactList,
  type ArtifactData,
  type ArtifactType,
  type ArtifactStatus,
} from "./ArtifactCard";

// Phase 1 Frontend Style Guide Components
export {
  AgentStatusStream,
  type AgentStatus,
  type AgentStatusStreamState,
  type ToolExecutionInfo,
  type RetrievalInfo,
} from "./AgentStatusStream";

export {
  CitationDrawer,
  CitationBadge,
  InlineCitations,
  useCitationDrawer,
  type CitationDrawerProps,
  type CitationBadgeProps,
  type InlineCitationsProps,
  type UseCitationDrawerOptions,
} from "./CitationDrawer";

export {
  TaskResultCard,
  TaskResultList,
  type TaskResultCardProps,
  type TaskResultType,
  type TaskResultData,
  type CodeBlockData,
  type ImageData,
  type SummaryData,
  type TableData,
  type FileData,
} from "./TaskResultCard";

export {
  ErrorDisplay,
  type StreamError,
} from "./ErrorDisplay";
