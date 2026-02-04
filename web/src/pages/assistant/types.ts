/**
 * Type definitions for the Assistant page components.
 *
 * Phase 1: Unified session + message + streaming protocol.
 * - Enhanced message structure with tool_calls/tool_results/citations/attachments
 * - Standard SSE event types
 */

import type { WebSearchResult } from "@/api/assistant";
import type { FileUploadResponse } from "@/api/files";

// =============================================================================
// SSE Event Types (matches backend SSEEventType)
// =============================================================================

export const SSEEventType = {
  STARTED: "started",  // Immediate response to reduce first-token latency
  TEXT_DELTA: "text_delta",
  STATUS: "status",  // Added for agent thinking status
  THINKING_DELTA: "thinking_delta",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  CONTEXT_RETRIEVED: "context_retrieved",
  WEB_SEARCH_RESULTS: "web_search_results",
  FILE_PROCESSED: "file_processed",  // File upload processing complete
  RAG_EVALUATION: "rag_evaluation",  // Phase 3: RAG quality metrics
  CACHE_METRICS: "cache_metrics",  // KV-Cache optimization metrics
  SESSION_CREATED: "session_created",
  SESSION_UPDATED: "session_updated",
  USAGE: "usage",
  FINISH: "finish",
  DONE: "done",
  ERROR: "error",
  // Output validation
  OUTPUT_WARNINGS: "output_warnings",
  // Code execution events
  CODE_EXECUTION_START: "code_execution_start",
  CODE_EXECUTION_OUTPUT: "code_execution_output",
  CODE_EXECUTION_RESULT: "code_execution_result",
  // Document/Image generation events
  IMAGE_GENERATION_START: "image_generation_start",
  IMAGE_GENERATION_RESULT: "image_generation_result",
  DOCUMENT_GENERATION_START: "document_generation_start",
  DOCUMENT_GENERATION_RESULT: "document_generation_result",
  // Manus-style PPT outline events
  OUTLINE_READY: "outline_ready",  // Slide outline parsed and ready for preview
  // Artifact events
  ARTIFACT_CREATED: "artifact_created",
  // Agentic workflow events
  WORKING_MEMORY_UPDATE: "working_memory_update",
  TASK_PLANNING: "task_planning",
  MEMORY_LOADED: "memory_loaded",
  TOOL_ERROR: "tool_error",
  // AG-UI step events
  STEP_STARTED: "step_started",
  STEP_FINISHED: "step_finished",
  RUN_STARTED: "run_started",
  RUN_FINISHED: "run_finished",
  RUN_ERROR: "run_error",
} as const;

export type SSEEventTypeValue = (typeof SSEEventType)[keyof typeof SSEEventType];

// =============================================================================
// Tool Call Structures (for Agentic workflows)
// =============================================================================

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  status: "pending" | "running" | "completed" | "error";
}

export interface ToolResult {
  tool_call_id: string;
  name: string;
  result: unknown;
  error?: string;
  duration_ms?: number;
}

// =============================================================================
// Citation Structures (for RAG traceability)
// =============================================================================

export interface Citation {
  dataset_id: string;
  document_id?: string;
  chunk_id?: string;
  source_url?: string;
  title?: string;
  score?: number;
  content_preview?: string;
}

// =============================================================================
// Phase 3: Enhanced Citation with Status
// =============================================================================

export type CitationStatus = "used" | "implicit" | "unused";

export interface RAGCitation {
  citation_id: string;
  chunk_id: string;
  dataset_id: string;
  dataset_name: string;
  source_url?: string;
  source_title?: string;
  cited_text: string;
  context_preview: string;
  relevance_score: number;
  status: CitationStatus;
}

export interface RAGQualityBreakdown {
  relevance: number;  // 0-25
  coverage: number;   // 0-25
  usage: number;      // 0-25
  citations: number;  // 0-25
}

export interface RAGEvaluation {
  quality_score: number;  // 0-100
  quality_breakdown: RAGQualityBreakdown;
  chunks_retrieved: number;
  chunks_used: number;
  response_grounding: number;  // 0-1
  citations: RAGCitation[];
  evaluation_time_ms: number;
}

// =============================================================================
// KV-Cache Metrics
// =============================================================================

export interface CacheMetrics {
  layer1_hit: boolean;
  layer2_hit: boolean;
  total_input_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
  estimated_savings_usd: number;
  system_prefix_hash: string;
}

// =============================================================================
// Attachment Structures (for multimodal input)
// =============================================================================

export interface MessageAttachment {
  id: string;
  type: "image" | "file" | "audio";
  filename?: string;
  url?: string;
  mime_type?: string;
  size_bytes?: number;
}

// =============================================================================
// Context Structures (for RAG display)
// =============================================================================

export interface RetrievedChunk {
  content: string;
  score: number;
  metadata?: Record<string, unknown>;
  source_url?: string;
  image_url?: string;
}

export interface RetrievedContext {
  dataset_id: string;
  dataset_name: string;
  chunks: RetrievedChunk[];
  query: string;
  took_ms: number;
}

// =============================================================================
// Search Status (for GPT-like "Searching..." display)
// =============================================================================

export type SearchType = "kb" | "web" | "files";
export type SearchState = "searching" | "completed" | "error";

export interface SearchStatusItem {
  type: SearchType;
  state: SearchState;
  query?: string;
  datasets?: string[];
  resultCount?: number;
  durationMs?: number;
  error?: string;
}

// =============================================================================
// Generated Artifact (for inline document preview in messages)
// =============================================================================

export interface GeneratedArtifact {
  id: string;
  type: "document" | "image" | "code" | "file";
  format: string;
  title: string;
  url: string;
  filename?: string;
  mimeType?: string;
  sizeBytes?: number;
  content?: string;  // For markdown preview
}

// =============================================================================
// Enhanced Chat Message (Phase 1)
// =============================================================================

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;

  // Streaming state
  isStreaming?: boolean;

  // Image generation state (GPT-style)
  isGeneratingImage?: boolean;
  imageGenerationPrompt?: string;

  // Search status (for GPT-like "Searching..." display)
  searchStatus?: SearchStatusItem[];

  // Agent phase status (for ReAct thinking display)
  agentPhase?: AgentPhaseStatus;

  // Agentic extensions
  toolCalls?: ToolCall[];
  toolResults?: ToolResult[];

  // RAG context
  contexts?: RetrievedContext[];
  citations?: Citation[];

  // Phase 3: RAG evaluation and enhanced citations
  ragEvaluation?: RAGEvaluation;
  ragCitations?: RAGCitation[];

  // KV-Cache metrics
  cacheMetrics?: CacheMetrics;

  // Web search
  webSearchResults?: WebSearchResult[];

  // Multimodal
  attachments?: MessageAttachment[];

  // Generated artifacts (documents, images, etc.)
  generatedArtifacts?: GeneratedArtifact[];

  // Metadata
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  durationMs?: number;
  firstTokenMs?: number;
  modelId?: string;
  timestamp?: string;
}

// =============================================================================
// SSE Event Data Types
// =============================================================================

export interface ContextRetrievedEventData {
  dataset_id: string;
  dataset_name: string;
  chunks: RetrievedChunk[];
  query: string;
  took_ms: number;
}

export interface ToolCallEventData {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  status: string;
}

export interface ToolResultEventData {
  tool_call_id: string;
  name: string;
  result: unknown;
  error?: string;
  duration_ms?: number;
}

export interface UsageEventData {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
}

export interface DoneEventData {
  session_id: string;
  total_length: number;
  duration_ms: number;
  model_id: string;
  kb_datasets_used: string[];
  context_truncated: boolean;
  // Phase 3: RAG quality summary
  rag_quality?: number;
  citations_count?: number;
}

// Phase 3: RAG Evaluation Event Data
export interface RAGEvaluationEventData {
  quality_score: number;
  quality_breakdown: RAGQualityBreakdown;
  chunks_retrieved: number;
  chunks_used: number;
  response_grounding: number;
  citations: RAGCitation[];
  evaluation_time_ms: number;
}

// KV-Cache metrics event data
export interface CacheMetricsEventData {
  layer1_hit: boolean;
  layer2_hit: boolean;
  total_input_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
  estimated_savings_usd: number;
  system_prefix_hash: string;
}

export interface ErrorEventData {
  message: string;
  code?: string;
  recoverable: boolean;
}

// File processed event data (after backend processes uploaded files)
export interface FileProcessedEventData {
  image_count: number;
  text_length: number;
  description_count: number;
  requires_rag: boolean;
  files?: Array<{
    file_path: string;
    type: "image" | "document";
  }>;
}

// Task planning event data - sent when agent creates an execution plan
export interface TaskPlanningEventData {
  goal: string;
  tasks: Array<{
    id: string;
    description: string;
    type: string;
    dependencies: string[];
  }>;
  parallel_groups: string[][];
}

// Working memory update event data - sent during task execution to report progress
export interface WorkingMemoryUpdateEventData {
  goal?: string;
  tasks: Array<{
    id: string;
    description: string;
    status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'blocked';
    result?: string;
    error?: string;
  }>;
  collected_info: Array<{
    key: string;
    value: string;
    source: string;
  }>;
  notes: string[];
  progress: {
    total: number;
    completed: number;
    failed: number;
    percentage: number;
  };
}

// Memory loaded event data - sent when user preferences are loaded
export interface MemoryLoadedEventData {
  preferences_loaded: boolean;
}

// Tool error event data - sent when a tool execution fails
export interface ToolErrorEventData {
  tool_name: string;
  error_type: string;
  error_message: string;
  suggestion?: string;
  recoverable: boolean;
  context?: Record<string, unknown>;
}

// Output warnings event data - validation warnings about the response
export interface OutputWarningsEventData {
  warnings: Array<{
    type: string;
    message: string;
    severity: 'low' | 'medium' | 'high';
  }>;
}

export interface StreamEvent {
  event_type: SSEEventTypeValue;
  data: unknown;
  timestamp: number;
}

// =============================================================================
// File Upload Types
// =============================================================================

export interface UploadedFile {
  file: File;
  status: "pending" | "compressing" | "uploading" | "success" | "error";
  response?: FileUploadResponse;
  error?: string;
  progress?: number;
}

// =============================================================================
// UI Helper Types
// =============================================================================

export interface SuggestedPrompt {
  icon: React.ReactNode;
  titleKey: string;
  promptKey: string;
  categoryKey: string;
}

export interface SessionSummary {
  session_id: string;
  title?: string;
  message_count: number;
  created_at?: string;
  updated_at?: string;
  last_message_preview?: string;
}

// =============================================================================
// Code Execution State (for Artifacts Panel)
// =============================================================================

export interface OutputFile {
  filename: string;
  content_base64: string;
  mime_type: string | null;
  size_bytes: number;
}

export interface CodeExecutionState {
  isExecuting: boolean;
  executionId: string | null;
  code: string | null;
  output: string;
  executionTimeMs: number | null;
  status: "idle" | "running" | "success" | "error" | "timeout";
  outputFiles: OutputFile[];
}

// =============================================================================
// Artifact Data (for Artifacts Panel)
// =============================================================================

export interface ArtifactData {
  id: string;
  type: "code" | "chart" | "table" | "file";
  format: string;
  title: string;
  url?: string;
  content?: string;
  createdAt: Date;
}

// =============================================================================
// ReAct Phase Types (for Agent thinking status display)
// =============================================================================

export type ReActPhase =
  | "analyzing"    // Initial task analysis
  | "thinking"     // Reasoning about next step
  | "planning"     // Creating execution plan
  | "executing"    // Running a tool
  | "observing"    // Analyzing tool result
  | "writing"      // Generating content
  | "completing";  // Finishing up

export interface AgentPhaseStatus {
  phase: ReActPhase;
  message: string;
  isDocumentTask?: boolean;
  taskId?: string;
}

// =============================================================================
// Task Panel Types (for Agentic workflow progress)
// =============================================================================

export type AgentTaskStatus = "pending" | "in_progress" | "completed" | "failed";

export interface AgentTask {
  id: string;
  description: string;
  status: AgentTaskStatus;
  result?: string;
  error?: string;
}

export interface CollectedInfo {
  key: string;
  value: string;
  source: string;
}

// =============================================================================
// Parallel Execution Types (for Tool Execution Visualization)
// =============================================================================

export type ToolExecutionStatus = "pending" | "running" | "completed" | "failed";

export interface ToolExecution {
  id: string;
  tool: string;
  status: ToolExecutionStatus;
  progress?: number;
  duration?: number;
  result?: string;
  error?: string;
}

export interface ParallelGroup {
  groupId: number;
  executions: ToolExecution[];
}

export interface ParallelExecutionViewProps {
  groups: ParallelGroup[];
  currentGroup: number;
}

// =============================================================================
// Manus-style Slide Outline Types (for PPT preview)
// =============================================================================

export interface SlideOutlineItem {
  number: number;
  title: string;
  subtitle?: string;
  type: "title" | "content" | "two_column" | "section" | "blank";
  bulletCount?: number;
}

export interface SlideOutline {
  title: string;
  subtitle?: string;
  slides: SlideOutlineItem[];
  theme?: string;
  totalSlides: number;
}

export interface OutlineReadyEventData {
  outline: SlideOutline;
  format: "pptx" | "docx" | "pdf";
}

// Step event data for AG-UI workflow tracking
export interface StepStartedEventData {
  step_id: string;
  title: string;
  description?: string;
  icon?: string;
  timestamp: number;
}

export interface StepFinishedEventData {
  step_id: string;
  status: "completed" | "failed";
  duration_ms?: number;
  timestamp: number;
}

// =============================================================================
// Manus-style Agentic Task Types
// =============================================================================

export type ManusTaskStatus = "pending" | "in_progress" | "completed" | "failed" | "blocked" | "skipped";
export type ManusTaskIcon = "search" | "web" | "kb" | "code" | "image" | "doc" | "ppt" | "file" | "brain";

export interface ManusSubTask {
  id: string;
  label: string;
  status: ManusTaskStatus;
  icon?: ManusTaskIcon;
  durationMs?: number;
}

export interface ManusTask {
  id: string;
  title: string;
  description?: string;
  status: ManusTaskStatus;
  icon?: ManusTaskIcon;
  subTasks?: ManusSubTask[];
  result?: string;
  error?: string;
  durationMs?: number;
  // For slide outline preview
  slideOutline?: SlideOutline;
}

// =============================================================================
// Working Memory State (for useChatSession hook)
// =============================================================================

export interface WorkingMemoryTask {
  id: string;
  description: string;
  status: ManusTaskStatus;
  icon?: string;
  result?: string;
  error?: string;
  startTime?: number;
  endTime?: number;
  durationMs?: number;
  currentTool?: string;
  // Search-related fields
  searchStatus?: string;  // "searching" | "completed" | "error"
  searchQuery?: string;
  searchSource?: "kb" | "web" | "file";
  searchProgress?: number;
  searchResultsFound?: number;
  searchResultsCount?: number;
  searchDurationMs?: number;
  subTasks?: Array<{
    id: string;
    name?: string;
    label?: string;
    status: string;
    endTime?: number;
    durationMs?: number;
    searchResultsCount?: number;
    searchDurationMs?: number;
  }>;
  slideOutline?: SlideOutline;
}

export interface WorkingMemoryCollectedInfo {
  key: string;
  value: string;
  source: string;
}

export interface WorkingMemory {
  goal?: string;
  tasks: WorkingMemoryTask[];
  collectedInfo: WorkingMemoryCollectedInfo[];
  notes: string[];
  runId?: string;
  error?: string;
}
