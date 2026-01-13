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
  TEXT_DELTA: "text_delta",
  THINKING_DELTA: "thinking_delta",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  CONTEXT_RETRIEVED: "context_retrieved",
  WEB_SEARCH_RESULTS: "web_search_results",
  RAG_EVALUATION: "rag_evaluation",  // Phase 3: RAG quality metrics
  SESSION_CREATED: "session_created",
  SESSION_UPDATED: "session_updated",
  USAGE: "usage",
  FINISH: "finish",
  DONE: "done",
  ERROR: "error",
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

export type SearchType = "kb" | "web";
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
// Enhanced Chat Message (Phase 1)
// =============================================================================

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;

  // Streaming state
  isStreaming?: boolean;

  // Search status (for GPT-like "Searching..." display)
  searchStatus?: SearchStatusItem[];

  // Agentic extensions
  toolCalls?: ToolCall[];
  toolResults?: ToolResult[];

  // RAG context
  contexts?: RetrievedContext[];
  citations?: Citation[];

  // Phase 3: RAG evaluation and enhanced citations
  ragEvaluation?: RAGEvaluation;
  ragCitations?: RAGCitation[];

  // Web search
  webSearchResults?: WebSearchResult[];

  // Multimodal
  attachments?: MessageAttachment[];

  // Metadata
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  durationMs?: number;
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

export interface ErrorEventData {
  message: string;
  code?: string;
  recoverable: boolean;
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
  title: string;
  prompt: string;
  category: string;
}

export interface SessionSummary {
  session_id: string;
  title?: string;
  message_count: number;
  created_at?: string;
  updated_at?: string;
  last_message_preview?: string;
}
