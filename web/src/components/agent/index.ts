/**
 * Agent Components - Manus-style agent visualization
 *
 * This module provides components for visualizing agent execution,
 * including timeline, artifacts, and status displays.
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
