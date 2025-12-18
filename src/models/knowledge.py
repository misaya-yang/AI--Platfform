from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from .enums import DatasetPermission, DatasetVisibility, DocumentStatus


@dataclass
class Dataset:
    dataset_id: str
    name: str
    description: str = ""
    tenant_id: str = ""
    visibility: DatasetVisibility = DatasetVisibility.PRIVATE

    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    embedding_config: Dict[str, Any] = field(default_factory=dict)

    index_config: Dict[str, Any] = field(default_factory=dict)
    collection_name: Optional[str] = None

    created_by: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Document:
    document_id: str
    dataset_id: str
    title: str

    source_type: str = "upload"  # upload|text|url
    source_uri: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None

    status: DocumentStatus = DocumentStatus.UPLOADED
    progress: float = 0.0
    error: Optional[str] = None

    content: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class Segment:
    segment_id: str
    dataset_id: str
    document_id: str
    position: int
    text: str

    token_count: int = 0
    vector_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DatasetPermissionBinding:
    dataset_id: str
    subject_type: str  # user|role
    subject_id: str
    permission: DatasetPermission = DatasetPermission.VIEWER

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
