from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class DatasetCreateSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    dataset_id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    visibility: str = "private"  # private|tenant|public

    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: Optional[int] = None
    embedding_config: Dict[str, Any] = Field(default_factory=dict)

    index_config: Dict[str, Any] = Field(default_factory=dict)
    collection_name: Optional[str] = None


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


class DatasetPermissionGrantSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    subject_type: str  # user|role
    subject_id: str
    permission: str  # owner|editor|viewer


class DocumentCreateTextSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


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
