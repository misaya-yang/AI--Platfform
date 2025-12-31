"""
Confluence Data Models.

Defines data structures for Confluence entities including pages,
spaces, and synchronization results.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ConfluenceCredentials:
    """Confluence 认证凭据"""
    domain: str  # e.g., 'yourcompany.atlassian.net'
    email: str
    api_token: str

    @property
    def base_url(self) -> str:
        """Wiki base URL"""
        return f"https://{self.domain}/wiki"

    @property
    def api_v2_url(self) -> str:
        """REST API v2 base URL"""
        return f"{self.base_url}/api/v2"

    @property
    def api_v1_url(self) -> str:
        """REST API v1 base URL (for some legacy endpoints)"""
        return f"{self.base_url}/rest/api"


@dataclass
class ConfluencePage:
    """Confluence 页面数据"""
    page_id: str
    space_key: str
    title: str
    version: int
    body_storage: str  # Storage format (XHTML-like)
    body_text: Optional[str] = None  # Plain text (if converted)
    parent_id: Optional[str] = None
    web_url: Optional[str] = None
    author_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    space_id: Optional[str] = None

    @property
    def content_hash(self) -> str:
        """计算内容哈希用于变更检测"""
        content = f"{self.title}:{self.version}:{self.body_storage}"
        return hashlib.sha256(content.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "page_id": self.page_id,
            "space_key": self.space_key,
            "title": self.title,
            "version": self.version,
            "body_storage": self.body_storage,
            "body_text": self.body_text,
            "parent_id": self.parent_id,
            "web_url": self.web_url,
            "author_id": self.author_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "labels": self.labels,
            "space_id": self.space_id,
            "content_hash": self.content_hash,
        }


@dataclass
class ConfluenceSpace:
    """Confluence 空间数据"""
    space_id: str
    space_key: str
    name: str
    type: str  # global, personal
    status: str  # current, archived
    homepage_id: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "space_id": self.space_id,
            "space_key": self.space_key,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "homepage_id": self.homepage_id,
            "description": self.description,
        }


@dataclass
class SyncResult:
    """同步结果"""
    total_pages: int = 0
    synced_pages: int = 0
    skipped_pages: int = 0
    failed_pages: int = 0
    created_documents: List[str] = field(default_factory=list)
    updated_documents: List[str] = field(default_factory=list)
    deleted_documents: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    task_id: Optional[str] = None

    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_pages == 0:
            return 0.0
        return (self.synced_pages + self.skipped_pages) / self.total_pages

    @property
    def has_errors(self) -> bool:
        """是否有错误"""
        return len(self.errors) > 0 or self.failed_pages > 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "total_pages": self.total_pages,
            "synced_pages": self.synced_pages,
            "skipped_pages": self.skipped_pages,
            "failed_pages": self.failed_pages,
            "created_documents": self.created_documents,
            "updated_documents": self.updated_documents,
            "deleted_documents": self.deleted_documents,
            "errors": self.errors,
            "success_rate": self.success_rate,
            "has_errors": self.has_errors,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class ConfluenceConnection:
    """Confluence 连接配置"""
    connection_id: str
    tenant_id: str
    name: str
    domain: str
    email: str
    api_token: str
    sync_mode: str = "manual"  # manual | polling
    polling_interval_minutes: int = 60
    status: str = "active"  # active | disabled | error
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_credentials(self) -> ConfluenceCredentials:
        """转换为认证凭据"""
        return ConfluenceCredentials(
            domain=self.domain,
            email=self.email,
            api_token=self.api_token,
        )


@dataclass
class ConfluenceSpaceBinding:
    """Confluence 空间绑定"""
    binding_id: str
    connection_id: str
    dataset_id: str
    space_key: str
    space_id: Optional[str] = None
    space_name: Optional[str] = None
    include_patterns: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)
    max_depth: int = 10
    include_attachments: bool = False
    include_comments: bool = False
    status: str = "pending"  # pending | syncing | completed | error
    last_sync_at: Optional[datetime] = None
    synced_page_count: int = 0
    total_page_count: int = 0
    last_error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class ConfluencePageRecord:
    """Confluence 页面同步记录"""
    id: str
    binding_id: str
    document_id: Optional[str]
    page_id: str
    space_key: str
    title: str
    version: int
    content_hash: Optional[str] = None
    parent_page_id: Optional[str] = None
    depth: int = 0
    status: str = "pending"  # pending | synced | error | deleted
    last_synced_at: Optional[datetime] = None
    confluence_updated_at: Optional[datetime] = None
    error: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    web_url: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class ConfluenceSyncTask:
    """Confluence 同步任务"""
    task_id: str
    binding_id: Optional[str]
    page_id: Optional[str]
    task_type: str  # full_sync | incremental_sync | page_sync | page_delete
    priority: int = 0
    status: str = "pending"  # pending | processing | completed | failed
    retry_count: int = 0
    max_retries: int = 3
    progress: float = 0.0
    total_items: int = 0
    processed_items: int = 0
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
