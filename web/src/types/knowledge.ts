/**
 * Production-grade Knowledge Base Type Definitions
 * 
 * Aligned with:
 * - Alibaba Cloud Knowledge Base
 * - Dify Knowledge Base
 * - Custom RAG pipeline requirements
 */

// ============================================================
// Base Types
// ============================================================

export type RetrieveMode = "keyword" | "hybrid" | "vector" | "dense" | "bm25";
export type ChunkingMode = "automatic" | "fixed_size" | "paragraph" | "page" | "heading" | "regex" | "separator" | "recursive" | "hierarchical" | "qa" | "islamic";
export type FusionStrategy = "rrf" | "weighted";
export type Visibility = "private" | "tenant" | "public";
export type DocumentStatus = "pending" | "parsing" | "segmenting" | "embedding" | "completed" | "failed";
export type KBType = "document" | "data" | "image" | "audio_video";
export type UseCase = "basic_qa" | "rich_text_response";

// ============================================================
// Dataset Types
// ============================================================

export interface Dataset {
  dataset_id: string;
  name: string;
  description?: string;
  tenant_id?: string;
  visibility: Visibility;
  kb_type?: KBType;  // document | data | image | audio_video
  use_case?: UseCase;  // basic_qa | rich_text_response
  embedding_provider: string;
  embedding_model: string;
  embedding_dimension?: number;
  embedding_config?: Record<string, unknown>;
  index_config?: IndexConfig;
  collection_name?: string;
  indexing_technique?: string;
  created_at?: string;
  updated_at?: string;
  created_by?: string;
  my_permission?: string;
  statistics?: DatasetStatistics;
}

export interface IndexConfig {
  chunking?: ChunkingConfig;
  retrieval?: RetrievalConfig;
}

// ============================================================
// Document Types
// ============================================================

export interface Document {
  document_id: string;
  dataset_id: string;
  title: string;
  content?: string;
  doc_type?: string;
  doc_form?: string;
  doc_language?: string;
  status: DocumentStatus;
  progress?: number;
  error?: string;
  segment_count?: number;
  word_count?: number;
  char_count?: number;
  size_bytes?: number;
  hit_count?: number;
  enabled?: boolean;
  archived?: boolean;
  metadata?: Record<string, unknown>;
  source_type?: "upload" | "url" | "confluence";
  created_at?: string;
  updated_at?: string;
  created_by?: string;
}

// ============================================================
// Segment Types
// ============================================================

export interface Segment {
  segment_id: string;
  dataset_id: string;
  document_id: string;
  position: number;
  text: string;
  token_count?: number;
  word_count?: number;
  char_count?: number;
  vector_id?: string;
  enabled?: boolean;
  status?: string;
  metadata?: Record<string, unknown>;
  keywords?: string[];
  answer?: string;
  hit_count?: number;
  created_at?: string;
  created_by?: string;
  // Hierarchical segment fields
  level?: 1 | 2 | 3; // 1=document, 2=section(parent), 3=paragraph(child)
  parent_segment_id?: string;
  summary?: string;
  // Image segment fields
  content_type?: "text" | "image";
  image_url?: string;
  image_attachment_id?: string;
  image_filename?: string;
  image_media_type?: string;
  image_file_size?: number;
}

// ============================================================
// Chunking Configuration Types
// ============================================================

export interface ChunkingConfig {
  mode: ChunkingMode;
  chunk_size: number;
  chunk_overlap: number;
  max_chunk_size?: number;
  min_chunk_size?: number;
  strict_section_traceability?: boolean;
  
  // Token-based
  use_token_count?: boolean;
  token_limit?: number;
  
  // Separators (for separator mode)
  separators?: string[];
  primary_separator?: string;
  keep_separator?: boolean;
  
  // Regex mode
  regex_pattern?: string;
  
  // Heading mode
  heading_patterns?: string[];
  heading_level?: "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
  
  // Paragraph mode
  min_paragraph_length?: number;
  merge_short_paragraphs?: boolean;
  
  // Hierarchical mode
  parent_chunk_size?: number;
  parent_overlap?: number;
  child_chunk_size?: number;
  child_overlap?: number;
  parent_mode?: "full_doc" | "paragraph" | "section";
  
  // QA mode
  question_prefix?: string;
  answer_prefix?: string;
  
  // Preprocessing
  remove_extra_spaces?: boolean;
  remove_urls_emails?: boolean;
  normalize_whitespace?: boolean;
  strip_html?: boolean;
  
  // Metadata extraction
  extract_metadata?: boolean;
  metadata_fields?: string[];
}

// ============================================================
// Metadata Enhancement Configuration Types
// ============================================================

export interface MetadataEnhancementConfig {
  enabled: boolean;
  extract_title?: boolean;
  extract_summary?: boolean;
  extract_keywords?: boolean;
  extract_entities?: boolean;
  detect_language?: boolean;
}

// ============================================================
// Table Processing Configuration Types
// ============================================================

export interface TableProcessingConfig {
  enabled: boolean;
  mode?: "markdown" | "row_based" | "cell_based" | "structured" | "natural_language";
  include_headers?: boolean;
  generate_summary?: boolean;
  max_rows?: number;
}

// ============================================================
// Retrieval Configuration Types
// ============================================================

export interface VectorRetrievalConfig {
  enabled: boolean;
  top_k: number;
  score_threshold?: number;
}

export interface KeywordRetrievalConfig {
  enabled: boolean;
  top_k: number;
  candidate_pool_size?: number;
  bm25_k1?: number;
  bm25_b?: number;
}

export interface FusionConfig {
  strategy: FusionStrategy;
  rrf_k: number;
  rrf_weights?: Record<string, number>;
  alpha: number;
}

export interface RerankConfig {
  enabled: boolean;
  provider?: "dashscope" | "cohere" | "jina" | "bge";
  model: string;
  top_n?: number;
  score_threshold?: number;
}

export interface MMRConfig {
  enabled: boolean;
  lambda: number;
  similarity_threshold?: number;
}

export interface IslamicEnhancementConfig {
  multi_query?: boolean;        // PRE_RETRIEVAL: expand query with Islamic synonyms
  citation_format?: boolean;    // POST_RANKING: attach formatted citations
  authority_sort?: boolean;     // POST_RANKING: sort by Quran > Hadith > Tafseer > Fiqh
  contextual_prefix?: boolean;  // INDEX-TIME: prepend context prefix (requires re-index)
  strict_section_traceability?: boolean; // INDEX-TIME: enforce section_title on every chunk
  max_expanded_queries?: number; // Max queries for multi_query (default 3)
}

export interface RetrievalConfig {
  mode: RetrieveMode;
  top_k: number;
  score_threshold?: number;

  vector?: VectorRetrievalConfig;
  keyword?: KeywordRetrievalConfig;
  fusion?: FusionConfig;
  rerank?: RerankConfig;
  mmr?: MMRConfig;
  islamic?: IslamicEnhancementConfig;
}

// Flat retrieval config for API requests
export interface FlatRetrievalConfig {
  mode?: RetrieveMode | "dense" | "bm25" | "hybrid";  // Support new mode names
  top_k?: number;
  score_threshold?: number;
  vector_top_k?: number;
  keyword_top_k?: number;
  candidate_top_k?: number;
  keyword_candidate_k?: number;
  // Fusion parameters
  fusion?: FusionStrategy;
  fusion_method?: "weighted" | "rrf";  // New: explicit fusion method
  dense_weight?: number;   // New: [0, 1] weight for dense scores
  bm25_weight?: number;    // New: [0, 1] weight for BM25 scores
  rrf_k?: number;
  alpha?: number;  // Legacy: converted to weights
  // Post-processing
  rerank?: boolean;
  rerank_model?: string;
  rerank_top_n?: number;
  mmr?: boolean;
  mmr_lambda?: number;
  mmr_threshold?: number;
}

// ============================================================
// Retrieval Request/Response Types
// ============================================================

export interface RetrieveRequest extends FlatRetrievalConfig {
  query: string;
  document_id?: string | null;
  rrf_weights?: Record<string, number>;
}

export interface RetrieveHit {
  segment_id: string;
  document_id: string;
  text: string;
  score: number;
  metadata?: Record<string, unknown>;
}

export interface RetrieveResponse {
  results: RetrieveHit[];
  metadata: {
    mode?: string;
    top_k?: number;
    score_threshold?: number;
    vector_hits_count?: number;
    vector_hits_raw_count?: number;  // Before filtering
    keyword_hits_count?: number;
    keyword_hits_raw_count?: number;  // Before filtering
    rerank?: boolean;
    mmr?: boolean;
    collection_name?: string;
    error?: string;
    [key: string]: unknown;
  };
}

// ============================================================
// QA Testing Types
// ============================================================

export interface LLMConfig {
  provider: "gemini" | "deepseek" | "dashscope" | "openai";
  model: string;
  api_key?: string;
  base_url?: string;
  temperature?: number;
  max_tokens?: number;
  system_prompt?: string;
}

export interface QARequest {
  query: string;
  top_k?: number;
  mode?: RetrieveMode;
  fusion_method?: "weighted" | "rrf";
  dense_weight?: number;
  bm25_weight?: number;
  document_id?: string | null;
  rerank?: boolean;
  mmr?: boolean;
  llm_config?: LLMConfig;
  include_raw_results?: boolean;
}

export interface QAContextSegment {
  segment_id: string;
  document_id: string;
  text: string;
  score: number;
  metadata?: Record<string, unknown>;
}

export interface QAResponse {
  query: string;
  answer: string;
  context_segments: QAContextSegment[];
  retrieval_metadata: Record<string, unknown>;
  timing: {
    retrieval_ms: number;
    llm_ms: number;
    total_ms: number;
  };
  model: string;
  tokens_used?: number;
}

export type QAStreamEvent =
  | {
      event: "retrieval";
      data: {
        query: string;
        context_segments: QAContextSegment[];
        retrieval_metadata: Record<string, unknown>;
        timing: {
          retrieval_ms: number;
        };
      };
    }
  | {
      event: "delta";
      data: {
        content: string;
      };
    }
  | {
      event: "done";
      data: {
        result: QAResponse;
      };
    }
  | {
      event: "error";
      data: {
        message: string;
      };
    };

export interface QATestCase {
  query: string;
  expected_answer?: string;
  expected_segments?: string[];
}

export interface QATestResult {
  test_case: QATestCase;
  result: QAResponse;
  answer_correct?: boolean;
  retrieval_recall?: number;
  retrieval_precision?: number;
}

export interface QABatchTestResult {
  results: QATestResult[];
  summary: {
    total_tests: number;
    average_retrieval_ms: number;
    average_llm_ms: number;
    average_recall?: number;
    average_precision?: number;
  };
}

// ============================================================
// Dataset Configuration Types
// ============================================================

export interface DatasetStatistics {
  document_count: number;
  available_document_count: number;
  segment_count: number;
  available_segment_count: number;
  word_count: number;
  hit_count: number;
}

export interface DatasetConfig {
  dataset_id: string;
  chunking: ChunkingConfig;
  retrieval: RetrievalConfig;
  embedding: {
    provider: string;
    model: string;
    dimension?: number;
    collection_name?: string;
  };
  statistics?: DatasetStatistics;
}

export interface DatasetDebugInfo {
  dataset: {
    id: string;
    name: string;
    embedding_provider: string;
    embedding_model: string;
    embedding_dimension?: number;
    collection_name?: string;
  };
  statistics: DatasetStatistics;
  sample_segments: Array<{
    segment_id: string;
    document_id: string;
    text_preview: string;
    token_count: number;
    vector_id?: string;
  }>;
  has_segments: boolean;
  has_collection: boolean;
}

// ============================================================
// Process Rule Types (Dify-compatible)
// ============================================================

export interface PreProcessingRule {
  id: "remove_extra_spaces" | "remove_urls_emails" | "remove_stopwords";
  enabled: boolean;
}

export interface SegmentationConfig {
  separator: string;
  max_tokens: number;
  chunk_overlap: number;
}

export interface ProcessRule {
  mode: "automatic" | "custom" | "hierarchical";
  pre_processing_rules: PreProcessingRule[];
  segmentation: SegmentationConfig;
  parent_mode?: string;
  child_chunk_size?: number;
}

// ============================================================
// Create/Update Request Types
// ============================================================

export interface DatasetCreateRequest {
  name: string;
  description?: string;
  visibility?: Visibility;
  kb_type?: KBType;
  use_case?: UseCase;
  embedding_provider: string;
  embedding_model: string;
  embedding_dimension?: number;
  embedding_config?: Record<string, unknown>;
  index_config?: IndexConfig;
  indexing_technique?: "high_quality" | "economy";
}

export interface DocumentCreateTextRequest {
  title: string;
  content: string;
  metadata?: Record<string, unknown>;
  process_rule?: ProcessRule;
  doc_form?: "text_model" | "qa_model";
  doc_language?: string;
}

// ============================================================
// Batch Operation Types
// ============================================================

export interface BatchOperationResult {
  success_count: number;
  failed_count: number;
  failed_ids: string[];
  errors: Record<string, string>;
}

// ============================================================
// Default Configurations
// ============================================================

export const DEFAULT_CHUNKING_CONFIG: ChunkingConfig = {
  mode: "automatic",
  chunk_size: 500,
  chunk_overlap: 50,
  remove_extra_spaces: true,
  remove_urls_emails: false,
};

export const DEFAULT_RETRIEVAL_CONFIG: RetrievalConfig = {
  mode: "hybrid",
  top_k: 5,
  vector: {
    enabled: true,
    top_k: 20,
  },
  keyword: {
    enabled: true,
    top_k: 20,
  },
  fusion: {
    strategy: "rrf",
    rrf_k: 60,
    alpha: 0.75,
  },
  rerank: {
    enabled: false,
    model: "gte-rerank",
  },
  mmr: {
    enabled: false,
    lambda: 0.5,
  },
};

// ============================================================
// Helper Functions
// ============================================================

// NOTE: Use i18n keys (knowledge.chunkModeLabels.*, knowledge.retrieveModeLabels.*)
// instead of these deprecated helpers. They will be removed in a future release.

/** @deprecated Use i18n key knowledge.chunkModeLabels instead */
export function getChunkingModeLabel(mode: ChunkingMode): string {
  // Deprecated: use t("knowledge.chunkModeLabels.<mode>") from i18n
  return mode;
}

/** @deprecated Use i18n key knowledge.chunkModeDescriptions instead */
export function getChunkingModeDescription(mode: ChunkingMode): string {
  // Deprecated: use t("knowledge.chunkModeDescriptions.<mode>") from i18n
  void mode;
  return "";
}

/** @deprecated Use i18n key knowledge.retrieveModeLabels instead */
export function getRetrievalModeLabel(mode: RetrieveMode): string {
  // Deprecated: use t("knowledge.retrieveModeLabels.<mode>") from i18n
  return mode;
}
