from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from .enums import DatasetPermission, DatasetVisibility, DocumentStatus


class ProcessRuleMode(str, Enum):
    """Processing rule mode - matches Dify's DatasetProcessRule modes"""
    AUTOMATIC = "automatic"
    CUSTOM = "custom"
    HIERARCHICAL = "hierarchical"


class IndexingTechnique(str, Enum):
    """Indexing technique - high_quality uses embeddings, economy uses keywords only"""
    HIGH_QUALITY = "high_quality"
    ECONOMY = "economy"


class DocForm(str, Enum):
    """Document form - text_model or qa_model"""
    TEXT_MODEL = "text_model"
    QA_MODEL = "qa_model"


@dataclass
class PreProcessingRule:
    """Pre-processing rule for document cleaning"""
    id: str  # remove_extra_spaces|remove_urls_emails|remove_stopwords
    enabled: bool = True


@dataclass
class SegmentationConfig:
    """Segmentation configuration"""
    separator: str = "\n"
    max_tokens: int = 500
    chunk_overlap: int = 50


@dataclass
class ProcessRules:
    """Complete processing rules configuration"""
    pre_processing_rules: List[PreProcessingRule] = field(default_factory=list)
    segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    
    # Hierarchical mode specific
    parent_mode: Optional[str] = None  # paragraph|full_doc
    child_chunk_size: Optional[int] = None

    @classmethod
    def default_automatic(cls) -> "ProcessRules":
        return cls(
            pre_processing_rules=[
                PreProcessingRule(id="remove_extra_spaces", enabled=True),
                PreProcessingRule(id="remove_urls_emails", enabled=False),
            ],
            segmentation=SegmentationConfig(separator="\n", max_tokens=500, chunk_overlap=50),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pre_processing_rules": [
                {"id": r.id, "enabled": r.enabled} for r in self.pre_processing_rules
            ],
            "segmentation": {
                "separator": self.segmentation.separator,
                "max_tokens": self.segmentation.max_tokens,
                "chunk_overlap": self.segmentation.chunk_overlap,
            },
            "parent_mode": self.parent_mode,
            "child_chunk_size": self.child_chunk_size,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessRules":
        pre_rules = []
        for r in data.get("pre_processing_rules", []):
            pre_rules.append(PreProcessingRule(id=r.get("id", ""), enabled=r.get("enabled", True)))
        
        seg_data = data.get("segmentation", {})
        segmentation = SegmentationConfig(
            separator=seg_data.get("separator", "\n"),
            max_tokens=seg_data.get("max_tokens", 500),
            chunk_overlap=seg_data.get("chunk_overlap", 50),
        )
        
        return cls(
            pre_processing_rules=pre_rules,
            segmentation=segmentation,
            parent_mode=data.get("parent_mode"),
            child_chunk_size=data.get("child_chunk_size"),
        )


@dataclass
class DatasetProcessRule:
    """Dataset processing rule - similar to Dify's DatasetProcessRule"""
    id: str
    dataset_id: str
    mode: ProcessRuleMode = ProcessRuleMode.AUTOMATIC
    rules: ProcessRules = field(default_factory=ProcessRules.default_automatic)
    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Dataset:
    dataset_id: str
    name: str
    description: str = ""
    tenant_id: str = ""
    visibility: DatasetVisibility = DatasetVisibility.PRIVATE

    # Embedding configuration
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    embedding_config: Dict[str, Any] = field(default_factory=dict)

    # Indexing configuration
    index_config: Dict[str, Any] = field(default_factory=dict)
    collection_name: Optional[str] = None
    indexing_technique: IndexingTechnique = IndexingTechnique.HIGH_QUALITY

    # Statistics (Dify-style)
    document_count: int = 0
    segment_count: int = 0
    word_count: int = 0

    # UI/Display
    icon_info: Dict[str, Any] = field(default_factory=dict)

    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Document:
    document_id: str
    dataset_id: str
    title: str

    source_type: str = "upload"  # upload|text|url|notion_import|website_crawl
    source_uri: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None

    status: DocumentStatus = DocumentStatus.UPLOADED
    progress: float = 0.0
    error: Optional[str] = None

    content: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Enable/disable support (Dify-style)
    enabled: bool = True
    disabled_at: Optional[datetime] = None
    disabled_by: Optional[str] = None

    # Archive support
    archived: bool = False
    archived_reason: Optional[str] = None
    archived_by: Optional[str] = None
    archived_at: Optional[datetime] = None

    # Processing rule reference
    process_rule_id: Optional[str] = None

    # Statistics
    word_count: int = 0
    segment_count: int = 0
    tokens: int = 0

    # Batch operations
    batch: Optional[str] = None

    # Document type info
    doc_type: Optional[str] = None
    doc_form: DocForm = DocForm.TEXT_MODEL
    doc_language: Optional[str] = None

    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def display_status(self) -> str:
        """Get user-friendly display status (Dify-style)"""
        if self.status == DocumentStatus.UPLOADED:
            return "queuing"
        elif self.status in {DocumentStatus.PARSING, DocumentStatus.SEGMENTING, DocumentStatus.EMBEDDING}:
            return "indexing"
        elif self.status == DocumentStatus.FAILED:
            return "error"
        elif self.status == DocumentStatus.COMPLETED:
            if self.archived:
                return "archived"
            elif not self.enabled:
                return "disabled"
            else:
                return "available"
        return str(self.status.value)


@dataclass
class Segment:
    segment_id: str
    dataset_id: str
    document_id: str
    position: int
    text: str

    token_count: int = 0
    word_count: int = 0
    vector_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Enable/disable support
    enabled: bool = True
    disabled_at: Optional[datetime] = None
    disabled_by: Optional[str] = None

    # Status for async processing
    status: str = "completed"  # waiting|indexing|completed|error

    # Analytics
    hit_count: int = 0

    # Keywords for search enhancement
    keywords: List[str] = field(default_factory=list)

    # Q&A mode answer
    answer: Optional[str] = None

    # Vector store tracking
    index_node_id: Optional[str] = None
    index_node_hash: Optional[str] = None

    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ChildChunk:
    """Child chunk for hierarchical chunking mode"""
    id: str
    tenant_id: str
    dataset_id: str
    document_id: str
    segment_id: str
    position: int
    content: str
    word_count: int = 0
    
    index_node_id: Optional[str] = None
    index_node_hash: Optional[str] = None
    type: str = "automatic"  # automatic|manual
    
    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    indexing_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class DatasetQuery:
    """Query history for analytics"""
    id: str
    dataset_id: str
    content: str
    source: str = "api"  # api|hit_testing|app
    source_app_id: Optional[str] = None
    created_by_role: Optional[str] = None
    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DatasetPermissionBinding:
    dataset_id: str
    subject_type: str  # user|role
    subject_id: str
    permission: DatasetPermission = DatasetPermission.VIEWER

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
