from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Dataset Schemas
# ============================================================

class DatasetCreateSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    dataset_id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    visibility: str = "private"  # private|tenant|public

    embedding_provider: str = "local"
    embedding_model: str = "hash-384"
    embedding_dimension: Optional[int] = None
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


# ============================================================
# Process Rule Schemas (Dify-style)
# ============================================================

class PreProcessingRuleSchema(BaseModel):
    id: str  # remove_extra_spaces|remove_urls_emails|remove_stopwords
    enabled: bool = True


class SegmentationConfigSchema(BaseModel):
    separator: str = "\n"
    max_tokens: int = 500
    chunk_overlap: int = 50


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
    mode: str = "hybrid"  # keyword|hybrid|vector
    document_id: Optional[str] = None
    # Used when fusion=alpha; leave unset to use dataset defaults.
    alpha: Optional[float] = None

    # Advanced retrieval options (request-level overrides; dataset defaults in index_config.retrieval)
    vector_top_k: Optional[int] = None
    keyword_top_k: Optional[int] = None
    candidate_top_k: Optional[int] = None
    keyword_candidate_k: Optional[int] = None

    fusion: Optional[str] = None  # rrf|alpha (hybrid only)
    rrf_k: Optional[int] = None
    rrf_weights: Dict[str, float] = Field(default_factory=dict)

    rerank: Optional[bool] = None
    rerank_model: Optional[str] = None
    rerank_top_n: Optional[int] = None

    mmr: Optional[bool] = None
    mmr_lambda: Optional[float] = None
    mmr_threshold: Optional[float] = None


class RetrieveHitSchema(BaseModel):
    segment_id: str
    document_id: str
    score: float
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


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
