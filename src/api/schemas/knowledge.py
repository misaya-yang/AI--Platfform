from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Enums
# ============================================================

class HierarchicalStrategy(str, Enum):
    """Hierarchical retrieval strategies."""
    CASCADE = "cascade"
    PARALLEL = "parallel"
    ADAPTIVE = "adaptive"


# ============================================================
# Dataset Schemas
# ============================================================

class DatasetCreateSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    dataset_id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    visibility: str = "private"  # private|tenant|public

    # KB type and use case (multimodal support)
    kb_type: str = "document"  # document|data|image|audio_video
    use_case: str = "basic_qa"  # basic_qa|rich_text_response

    # Text-First RAG: Default to Gemini embedding
    embedding_provider: str = "gemini"  # gemini|dashscope|local
    embedding_model: str = "gemini-embedding-001"
    embedding_dimension: Optional[int] = 1024  # Gemini supports 768/1024/1536/3072
    embedding_config: Dict[str, Any] = Field(default_factory=dict)

    index_config: Dict[str, Any] = Field(default_factory=dict)
    collection_name: Optional[str] = None
    indexing_technique: str = "high_quality"  # high_quality|economy

    # Optional process rules
    process_rule: Optional[Dict[str, Any]] = None


class DatasetUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None
    kb_type: Optional[str] = None  # document|data|image|audio_video
    use_case: Optional[str] = None  # basic_qa|rich_text_response
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_dimension: Optional[int] = None
    embedding_config: Optional[Dict[str, Any]] = None
    index_config: Optional[Dict[str, Any]] = None
    indexing_technique: Optional[str] = None


class DatasetPermissionGrantSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    subject_type: str  # user|role
    subject_id: str
    permission: str  # owner|editor|viewer


class DatasetDeleteSchema(BaseModel):
    """Knowledge dataset delete confirmation payload."""
    model_config = ConfigDict(extra="forbid")

    password: str = Field(..., min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=500)


# ============================================================
# Process Rule Schemas (Dify-style)
# ============================================================

class PreProcessingRuleSchema(BaseModel):
    id: str  # remove_extra_spaces|remove_urls_emails|remove_stopwords
    enabled: bool = True


class SegmentationConfigSchema(BaseModel):
    separator: str = "\n"
    max_tokens: int = 500          # token limit for embedding models
    chunk_overlap: int = 50        # legacy overlap (tokens)


class ProcessRuleSchema(BaseModel):
    """Processing rule configuration - matches Dify's rule format"""
    model_config = ConfigDict(extra="allow")
    
    mode: str = "automatic"  # automatic|custom|hierarchical
    pre_processing_rules: List[PreProcessingRuleSchema] = Field(default_factory=list)
    segmentation: SegmentationConfigSchema = Field(default_factory=SegmentationConfigSchema)
    
    # Hierarchical mode specific
    parent_mode: Optional[str] = None  # paragraph|full_doc
    child_chunk_size: Optional[int] = None


# ============================================================
# Document Schemas
# ============================================================

class DocumentCreateTextSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # Optional process rule override
    process_rule: Optional[ProcessRuleSchema] = None
    doc_form: str = "text_model"  # text_model|qa_model
    doc_language: Optional[str] = None


class DocumentCreateUrlSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    title: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # Optional process rule override
    process_rule: Optional[ProcessRuleSchema] = None


class DocumentBatchCreateSchema(BaseModel):
    """Batch document creation schema"""
    model_config = ConfigDict(extra="allow")
    
    documents: List[DocumentCreateTextSchema] = Field(default_factory=list)
    process_rule: Optional[ProcessRuleSchema] = None
    batch_name: Optional[str] = None


class DocumentUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    doc_type: Optional[str] = None
    doc_language: Optional[str] = None


class DocumentEnableDisableSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    enabled: bool


class DocumentArchiveSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    archived: bool
    reason: Optional[str] = None


class RetrieveRequestSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    query: str
    top_k: int = 5
    mode: str = "hybrid"  # dense|bm25|hybrid (also accepts: keyword|vector for backwards compat)
    document_id: Optional[str] = None
    
    # Fusion weights for hybrid mode (0-1, sum to 1)
    dense_weight: Optional[float] = None   # Weight for dense (vector) scores
    bm25_weight: Optional[float] = None    # Weight for BM25 (keyword) scores
    fusion_method: Optional[str] = None    # "weighted" or "rrf"
    
    # Legacy alpha parameter (converted to weights: alpha = dense_weight)
    alpha: Optional[float] = None
    
    # Score threshold to filter out low-relevance results (0.0 - 1.0)
    # Default 0.0 means no filtering. Recommended: 0.3-0.5 for keyword, 0.5-0.7 for vector
    score_threshold: Optional[float] = None

    # Advanced retrieval options (request-level overrides; dataset defaults in index_config.retrieval)
    vector_top_k: Optional[int] = None
    keyword_top_k: Optional[int] = None
    candidate_top_k: Optional[int] = None
    keyword_candidate_k: Optional[int] = None

    fusion: Optional[str] = None  # Legacy: rrf|alpha (hybrid only)
    rrf_k: Optional[int] = None
    rrf_weights: Dict[str, float] = Field(default_factory=dict)  # Legacy

    rerank: Optional[bool] = None
    rerank_model: Optional[str] = None
    rerank_top_n: Optional[int] = None

    mmr: Optional[bool] = None
    mmr_lambda: Optional[float] = None
    mmr_threshold: Optional[float] = None

    # Multimodal retrieval options
    include_images: bool = True  # Include image segments in results
    include_associated_images: bool = True  # Attach associated images to text segments
    multimodal_rerank: bool = False  # Use VLM for multimodal reranking
    content_type_filter: Optional[str] = None  # Filter by content type: text|image|None (all)

    # Advanced multimodal parameters (Phase 2 optimization)
    image_search_enabled: bool = True  # Directly search image segments (not just associated)
    vlm_rerank_weight: Optional[float] = None  # Weight of VLM score (0.0-1.0), default 0.4
    image_boost: Optional[float] = None  # Boost factor for image results (>1 = prefer images)
    image_score_threshold: Optional[float] = None  # Score threshold for images (lower than text)
    use_separate_thresholds: bool = False  # Use different thresholds for text vs image

    # Islamic knowledge traceability filters
    source_type_filter: Optional[str] = None  # Filter by source: quran|hadith|fiqh|tafseer
    language_filter: Optional[str] = None     # Filter by language: ar|en|ar_en
    multi_query: bool = False                 # Enable Islamic multi-query expansion
    authority_sort: bool = False              # Sort results by Islamic authority (Quran > Hadith > Fiqh)

    # Hierarchical retrieval options (for large document collections)
    hierarchical: bool = False                 # Enable hierarchical 3-level retrieval
    hierarchical_strategy: HierarchicalStrategy = HierarchicalStrategy.CASCADE
    l1_top_k: int = 5                          # Documents to consider at L1 (summary level)
    l2_top_k: int = 10                         # Sections to consider at L2 (section level)
    include_context: bool = True               # Include parent context in L3 results


class AssociatedImageSchema(BaseModel):
    """Schema for associated image in retrieval results."""
    image_segment_id: str
    storage_url: str
    filename: str = ""
    vlm_description: Optional[str] = None
    proximity_score: float = 1.0
    media_type: str = "image/png"


class RetrieveHitSchema(BaseModel):
    segment_id: str
    document_id: str
    score: float
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # P3: Multimodal fields
    content_type: str = "text"  # "text" | "image"
    image_url: Optional[str] = None  # For image segments
    vlm_description: Optional[str] = None  # VLM-generated description for images
    associated_images: List[AssociatedImageSchema] = Field(default_factory=list)  # Associated images for text segments

    # Islamic knowledge traceability fields
    source_type: Optional[str] = None  # quran|hadith|tafseer|fiqh|general_islamic
    citation_text: Optional[str] = None  # Pre-formatted citation string
    source_reference: Dict[str, Any] = Field(default_factory=dict)  # Structured reference data


class RetrieveResponseSchema(BaseModel):
    results: List[RetrieveHitSchema] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SegmentUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str
    answer: Optional[str] = None  # For Q&A mode
    keywords: Optional[List[str]] = None


class SegmentCreateSchema(BaseModel):
    """Create a new segment manually"""
    model_config = ConfigDict(extra="allow")
    
    content: str
    answer: Optional[str] = None  # For Q&A mode
    keywords: Optional[List[str]] = None


class SegmentEnableDisableSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    enabled: bool


class SegmentBatchEnableDisableSchema(BaseModel):
    """Batch enable/disable segments"""
    model_config = ConfigDict(extra="allow")
    
    segment_ids: List[str]
    enabled: bool


# ============================================================
# Statistics/Info Schemas
# ============================================================

class DatasetStatisticsSchema(BaseModel):
    """Dataset statistics response"""
    dataset_id: str
    document_count: int = 0
    segment_count: int = 0
    word_count: int = 0
    available_document_count: int = 0
    available_segment_count: int = 0
    
    
class DocumentStatisticsSchema(BaseModel):
    """Document statistics response"""
    document_id: str
    segment_count: int = 0
    word_count: int = 0
    hit_count: int = 0


# ============================================================
# Query History Schemas
# ============================================================

class QueryHistorySchema(BaseModel):
    """Query history entry"""
    id: str
    dataset_id: str
    content: str
    source: str
    source_app_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str


class QueryHistoryListSchema(BaseModel):
    """Query history list response"""
    queries: List[QueryHistorySchema] = Field(default_factory=list)
    total: int = 0


# ============================================================
# Batch Operation Schemas
# ============================================================

class BatchReindexSchema(BaseModel):
    """Batch reindex documents"""
    model_config = ConfigDict(extra="allow")
    
    document_ids: List[str] = Field(default_factory=list)
    all_documents: bool = False  # If true, reindex all documents in dataset


class BatchDeleteSchema(BaseModel):
    """Batch delete documents"""
    model_config = ConfigDict(extra="allow")
    
    document_ids: List[str]


class BatchOperationResultSchema(BaseModel):
    """Batch operation result"""
    success_count: int = 0
    failed_count: int = 0
    failed_ids: List[str] = Field(default_factory=list)
    errors: Dict[str, str] = Field(default_factory=dict)


# ============================================================
# QA Testing Schemas
# ============================================================

class LLMConfigSchema(BaseModel):
    """LLM configuration for QA testing"""
    model_config = ConfigDict(extra="allow")
    
    provider: str = "deepseek"  # deepseek | gemini | dashscope
    model: str = "deepseek-chat"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 2048
    system_prompt: Optional[str] = None


class QAQuerySchema(BaseModel):
    """Request schema for QA query"""
    model_config = ConfigDict(extra="allow")
    
    query: str
    top_k: int = 5
    mode: str = "hybrid"
    fusion_method: Optional[str] = None
    dense_weight: Optional[float] = None
    bm25_weight: Optional[float] = None
    score_threshold: Optional[float] = None
    document_id: Optional[str] = None
    rerank: bool = False
    rerank_top_n: Optional[int] = None
    mmr: bool = False
    mmr_lambda: Optional[float] = None
    
    # LLM config override
    llm_config: Optional[LLMConfigSchema] = None
    
    # Include raw retrieval results
    include_raw_results: bool = False


class QAResultSchema(BaseModel):
    """Response schema for QA query"""
    query: str
    answer: str
    context_segments: List[Dict[str, Any]] = Field(default_factory=list)
    retrieval_metadata: Dict[str, Any] = Field(default_factory=dict)
    timing: Dict[str, int] = Field(default_factory=dict)
    model: str
    tokens_used: Optional[int] = None


class QATestCaseSchema(BaseModel):
    """Test case for QA evaluation"""
    model_config = ConfigDict(extra="allow")
    
    query: str
    expected_answer: Optional[str] = None
    expected_segments: Optional[List[str]] = None  # segment_ids


class QABatchTestSchema(BaseModel):
    """Batch QA test request"""
    model_config = ConfigDict(extra="allow")
    
    test_cases: List[QATestCaseSchema]
    top_k: int = 5
    mode: str = "hybrid"
    rerank: bool = False
    mmr: bool = False
    
    # LLM config
    llm_config: Optional[LLMConfigSchema] = None


class QATestResultSchema(BaseModel):
    """Single test result"""
    test_case: QATestCaseSchema
    result: QAResultSchema
    answer_correct: Optional[bool] = None
    retrieval_recall: Optional[float] = None
    retrieval_precision: Optional[float] = None


class QABatchTestResultSchema(BaseModel):
    """Batch test results"""
    results: List[QATestResultSchema] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


# ============================================================
# Batch Retrieval Schemas
# ============================================================

class BatchRetrieveRequestSchema(BaseModel):
    """Batch retrieval request - parallel retrieval with multiple queries.

    Usage:
    - queries: List of queries to retrieve in parallel
    - Or use query with comma-separated queries: "query1,query2,query3"

    Returns results grouped by query.
    """
    model_config = ConfigDict(extra="allow")

    queries: Optional[List[str]] = None  # List of queries for batch retrieval
    query: Optional[str] = None  # Single query or comma-separated queries
    top_k: int = 5  # Top-k per query
    mode: str = "hybrid"
    document_id: Optional[str] = None

    # Fusion weights
    dense_weight: Optional[float] = None
    bm25_weight: Optional[float] = None
    fusion_method: Optional[str] = None
    alpha: Optional[float] = None
    score_threshold: Optional[float] = None

    # Advanced options
    vector_top_k: Optional[int] = None
    keyword_top_k: Optional[int] = None
    candidate_top_k: Optional[int] = None
    keyword_candidate_k: Optional[int] = None
    fusion: Optional[str] = None
    rrf_k: Optional[int] = None
    rrf_weights: Dict[str, float] = Field(default_factory=dict)

    # Post-processing
    rerank: Optional[bool] = None
    rerank_model: Optional[str] = None
    rerank_top_n: Optional[int] = None
    mmr: Optional[bool] = None
    mmr_lambda: Optional[float] = None
    mmr_threshold: Optional[float] = None

    # Multimodal options
    include_images: bool = True
    include_associated_images: bool = True

    # Batch-specific options
    max_parallel: int = 10  # Max parallel queries (limit concurrency)
    dedupe_results: bool = False  # Remove duplicate segments across queries


class BatchRetrieveResultSchema(BaseModel):
    """Result for a single query in batch retrieval."""
    query: str
    results: List[Dict[str, Any]] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class BatchRetrieveResponseSchema(BaseModel):
    """Batch retrieval response with results grouped by query."""
    batch_results: List[BatchRetrieveResultSchema] = Field(default_factory=list)
    total_queries: int = 0
    total_results: int = 0
    execution_time_ms: float = 0.0


# ============================================================
# Chunking Configuration Schemas
# ============================================================

class ChunkingConfigSchema(BaseModel):
    """Chunking configuration schema
    
    Supports multiple chunking modes:
    - automatic: Smart detection of best strategy
    - fixed_size: Fixed character count with overlap
    - paragraph: Split by paragraphs
    - heading: Split by document headings
    - hierarchical: Parent-child dual-layer chunking
    - separator: Split by custom separators
    - regex: Split by regex pattern
    - recursive: Recursive multi-level splitting
    - qa: Question-answer pair format
    - islamic: Islamic text-aware chunking (Quran/Hadith/Fiqh/Tafseer)
    """
    model_config = ConfigDict(extra="allow")
    
    mode: str = "automatic"
    chunk_size: int = 2000       # ~400-500 tokens, matches ChunkingConfig default
    chunk_overlap: int = 300     # 15% overlap, matches ChunkingConfig default

    # Token-based (preferred for production)
    use_token_count: bool = True
    token_limit: Optional[int] = None
    min_chunk_tokens: Optional[int] = None
    max_chunk_tokens: Optional[int] = None
    parent_token_limit: Optional[int] = None
    child_token_limit: Optional[int] = None
    
    # Separator mode
    separator: str = "\n"
    primary_separator: Optional[str] = None
    keep_separator: Optional[bool] = None
    
    # Regex mode
    regex_pattern: Optional[str] = None
    
    # Heading mode
    heading_level: Optional[str] = None  # h1 | h2 | h3 etc
    heading_patterns: Optional[List[str]] = None
    
    # Paragraph mode
    min_paragraph_length: Optional[int] = None
    merge_short_paragraphs: Optional[bool] = None
    
    # Hierarchical mode
    parent_mode: Optional[str] = None  # paragraph | full_doc | section
    parent_chunk_size: Optional[int] = None
    child_chunk_size: Optional[int] = None
    child_overlap: Optional[int] = None
    
    # QA mode
    question_prefix: Optional[str] = None
    answer_prefix: Optional[str] = None
    
    # Islamic text options (for islamic mode)
    islamic_source_type: Optional[str] = None  # quran|hadith|fiqh|tafseer|auto
    preserve_verse_integrity: bool = True
    preserve_hadith_integrity: bool = True

    # Pre-processing
    remove_extra_spaces: bool = True
    remove_urls_emails: bool = False


class RetrievalConfigSchema(BaseModel):
    """Retrieval pipeline configuration schema"""
    model_config = ConfigDict(extra="allow")
    
    mode: str = "hybrid"  # vector | keyword | hybrid
    top_k: int = 5
    score_threshold: Optional[float] = None
    
    # Vector search
    vector_enabled: bool = True
    vector_top_k: int = 20
    
    # Keyword search
    keyword_enabled: bool = True
    keyword_top_k: int = 20
    
    # Fusion
    fusion_strategy: str = "rrf"  # rrf | weighted
    rrf_k: int = 60
    alpha: float = 0.75
    
    # Rerank
    rerank_enabled: bool = False
    rerank_model: str = "gte-rerank"
    rerank_top_n: Optional[int] = None
    
    # MMR
    mmr_enabled: bool = False
    mmr_lambda: float = 0.5




class DatasetConfigUpdateSchema(BaseModel):
    """Update dataset configuration (chunking + retrieval)"""
    model_config = ConfigDict(extra="allow")
    
    chunking_config: Optional[ChunkingConfigSchema] = None
    retrieval_config: Optional[RetrievalConfigSchema] = None


class ChunkPreviewRequestSchema(BaseModel):
    """Request schema for chunk preview"""
    model_config = ConfigDict(extra="allow")
    
    text: str
    config: Optional[ChunkingConfigSchema] = None
    document_id: Optional[str] = None


class ChunkPreviewItemSchema(BaseModel):
    """Single chunk item in preview"""
    content: str
    token_count: int
    char_count: int
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChunkPreviewResponseSchema(BaseModel):
    """Response schema for chunk preview"""
    chunks: List[ChunkPreviewItemSchema] = Field(default_factory=list)
    total_chunks: int = 0
