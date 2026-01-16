"""
Confluence Integration API Schemas.

Pydantic models for Confluence API requests and responses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Connection Schemas
# ============================================================

class ConfluenceConnectionCreateSchema(BaseModel):
    """Create a new Confluence connection"""
    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Connection name for display")
    domain: str = Field(..., description="Confluence domain (e.g., 'yourcompany.atlassian.net')")
    email: str = Field(..., description="User email for authentication")
    api_token: str = Field(..., description="Confluence API token")
    sync_mode: str = Field(default="manual", description="Sync mode: manual | polling")
    polling_interval_minutes: int = Field(default=60, ge=5, le=1440, description="Polling interval in minutes")


class ConfluenceConnectionUpdateSchema(BaseModel):
    """Update an existing Confluence connection"""
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    email: Optional[str] = None
    api_token: Optional[str] = None
    sync_mode: Optional[str] = None
    polling_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    status: Optional[str] = None


class ConfluenceConnectionResponseSchema(BaseModel):
    """Confluence connection response (without sensitive data)"""
    connection_id: str
    tenant_id: str
    name: str
    domain: str
    email: str
    sync_mode: str
    polling_interval_minutes: int
    status: str
    last_sync_at: Optional[str] = None
    last_error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ConfluenceConnectionTestResponseSchema(BaseModel):
    """Connection test result"""
    status: str  # success | error
    message: str
    spaces_available: Optional[bool] = None
    status_code: Optional[int] = None


# ============================================================
# Space Binding Schemas
# ============================================================

class ConfluenceSpaceBindingCreateSchema(BaseModel):
    """Bind a Confluence space to a dataset"""
    model_config = ConfigDict(extra="allow")

    dataset_id: str = Field(..., description="Target dataset ID")
    space_key: str = Field(..., description="Confluence space key (e.g., 'HFDSH')")
    root_page_id: Optional[str] = Field(default=None, description="Root page ID to sync from (deprecated, use root_page_ids)")
    root_page_ids: List[str] = Field(default_factory=list, description="List of root page IDs to sync (supports multi-select)")
    include_patterns: List[str] = Field(default_factory=list, description="Title patterns to include")
    exclude_patterns: List[str] = Field(default_factory=list, description="Title patterns to exclude")
    max_depth: int = Field(default=10, ge=1, le=100, description="Maximum page hierarchy depth")
    include_attachments: bool = Field(default=False, description="Include page attachments")
    include_comments: bool = Field(default=False, description="Include page comments")
    sync_images: bool = Field(default=True, description="Sync and embed images from pages (requires multimodal embedding)")
    image_max_size_bytes: int = Field(default=3 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024, description="Maximum image size in bytes (default 3MB)")


class ConfluenceSpaceBindingUpdateSchema(BaseModel):
    """Update a space binding (including sync mode configuration)"""
    model_config = ConfigDict(extra="allow")

    root_page_id: Optional[str] = Field(default=None, description="Root page ID (deprecated, use root_page_ids)")
    root_page_ids: Optional[List[str]] = Field(default=None, description="List of root page IDs (supports multi-select)")
    include_patterns: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None
    max_depth: Optional[int] = Field(default=None, ge=1, le=100)
    include_attachments: Optional[bool] = None
    include_comments: Optional[bool] = None
    # Sync mode configuration (binding-level)
    sync_mode: Optional[str] = Field(default=None, description="Sync mode: manual | polling")
    polling_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440, description="Polling interval in minutes")
    sync_enabled: Optional[bool] = Field(default=None, description="Enable/disable auto sync")
    # Image sync configuration
    sync_images: Optional[bool] = Field(default=None, description="Sync and embed images from pages")
    image_max_size_bytes: Optional[int] = Field(default=None, ge=1024, le=10 * 1024 * 1024, description="Maximum image size in bytes")


class ConfluenceSpaceBindingResponseSchema(BaseModel):
    """Space binding response"""
    binding_id: str
    connection_id: str
    tenant_id: Optional[str] = None
    dataset_id: str
    space_key: str
    space_id: Optional[str] = None
    space_name: Optional[str] = None
    root_page_id: Optional[str] = None
    root_page_ids: List[str] = Field(default_factory=list)
    root_page_title: Optional[str] = None
    root_page_titles: List[str] = Field(default_factory=list)
    include_patterns: List[str] = Field(default_factory=list)
    exclude_patterns: List[str] = Field(default_factory=list)
    max_depth: int
    include_attachments: bool
    include_comments: bool
    sync_images: bool = True
    image_max_size_bytes: int = 3 * 1024 * 1024
    status: str
    last_sync_at: Optional[str] = None
    synced_page_count: int = 0
    total_page_count: int = 0
    last_error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 创建绑定时自动触发的首次同步任务 ID
    initial_sync_task_id: Optional[str] = None
    # Sync mode configuration (binding-level)
    sync_mode: str = "manual"
    polling_interval_minutes: int = 60
    last_incremental_sync_at: Optional[str] = None
    sync_enabled: bool = True
    next_sync_at: Optional[str] = None


# ============================================================
# Space Discovery Schemas
# ============================================================

class ConfluenceSpaceInfoSchema(BaseModel):
    """Discovered space information"""
    space_id: str
    space_key: str
    name: str
    type: str  # global | personal
    status: str
    homepage_id: Optional[str] = None
    description: Optional[str] = None


class ConfluenceSpaceListResponseSchema(BaseModel):
    """List of discovered spaces"""
    spaces: List[ConfluenceSpaceInfoSchema] = Field(default_factory=list)
    total: int = 0


# ============================================================
# Page Tree Schemas (for folder/page hierarchy selection)
# ============================================================

class ConfluencePageTreeNodeSchema(BaseModel):
    """Page tree node for hierarchy display"""
    page_id: str
    title: str
    parent_id: Optional[str] = None
    has_children: bool = False
    children: List["ConfluencePageTreeNodeSchema"] = Field(default_factory=list)
    depth: int = 0
    web_url: Optional[str] = None


class ConfluencePageTreeResponseSchema(BaseModel):
    """Page tree response for space hierarchy"""
    space_key: str
    space_name: str
    root_pages: List[ConfluencePageTreeNodeSchema] = Field(default_factory=list)
    total_pages: int = 0


# ============================================================
# Import Schemas
# ============================================================

class ConfluenceUrlImportSchema(BaseModel):
    """Import a single page by URL"""
    model_config = ConfigDict(extra="allow")

    url: str = Field(..., description="Confluence page URL")
    dataset_id: str = Field(..., description="Target dataset ID")
    connection_id: str = Field(..., description="Confluence connection ID")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class ConfluenceSpaceImportSchema(BaseModel):
    """Trigger a full space import"""
    model_config = ConfigDict(extra="allow")

    binding_id: str = Field(..., description="Space binding ID")
    force_full_sync: bool = Field(default=False, description="Force full sync ignoring change detection")


class ConfluenceImportResultSchema(BaseModel):
    """Import operation result"""
    document_id: Optional[str] = None
    page_id: Optional[str] = None
    title: Optional[str] = None
    status: str  # created | updated | skipped | failed
    message: Optional[str] = None
    web_url: Optional[str] = None


# ============================================================
# Sync Status Schemas
# ============================================================

class ConfluenceSyncStatusSchema(BaseModel):
    """Sync operation status"""
    binding_id: str
    status: str  # pending | syncing | completed | error
    progress: float = 0.0
    total_pages: int = 0
    synced_pages: int = 0
    failed_pages: int = 0
    last_sync_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


class ConfluenceSyncTaskSchema(BaseModel):
    """Sync task details"""
    task_id: str
    binding_id: Optional[str] = None
    page_id: Optional[str] = None
    task_type: str
    priority: int
    status: str
    retry_count: int
    progress: float
    total_items: int
    processed_items: int
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: Optional[str] = None


class ConfluenceSyncTriggerSchema(BaseModel):
    """Trigger a sync operation"""
    model_config = ConfigDict(extra="allow")

    force: bool = Field(default=False, description="Force full sync")
    page_ids: Optional[List[str]] = Field(default=None, description="Specific page IDs to sync")


# ============================================================
# Page Record Schemas
# ============================================================

class ConfluencePageRecordSchema(BaseModel):
    """Synced page record"""
    id: str
    binding_id: str
    document_id: Optional[str] = None
    page_id: str
    space_key: str
    title: str
    version: int
    content_hash: Optional[str] = None
    parent_page_id: Optional[str] = None
    depth: int = 0
    status: str
    # effective_status: 计算后的有效状态
    # 当 status='synced' 但文档不存在或无 segment 时，返回 'needs_resync'
    effective_status: Optional[str] = None
    # 关联文档的处理状态
    document_status: Optional[str] = None
    document_progress: Optional[int] = None
    last_synced_at: Optional[str] = None
    confluence_updated_at: Optional[str] = None
    error: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    web_url: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Page-level sync configuration (overrides binding defaults)
    sync_mode: Optional[str] = None  # NULL=inherit, manual, polling
    polling_interval_minutes: Optional[int] = None
    sync_enabled: bool = True
    next_sync_at: Optional[str] = None
    sync_priority: int = 0


class ConfluencePageSyncConfigUpdateSchema(BaseModel):
    """Update page-level sync configuration"""
    model_config = ConfigDict(extra="allow")

    sync_mode: Optional[str] = Field(default=None, description="Sync mode: NULL(inherit) | manual | polling")
    polling_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    sync_enabled: Optional[bool] = None
    sync_priority: Optional[int] = Field(default=None, ge=0, le=100)


class ConfluencePageListResponseSchema(BaseModel):
    """List of page records"""
    pages: List[ConfluencePageRecordSchema] = Field(default_factory=list)
    total: int = 0
    synced: int = 0
    pending: int = 0
    error: int = 0
    # needs_resync: 需要重新同步的页面数量
    # 当页面 status='synced' 但关联文档不存在或无 segment 时
    needs_resync: int = 0


# ============================================================
# Scheduler Schemas
# ============================================================

class ConfluenceSchedulerStatusSchema(BaseModel):
    """Scheduler status"""
    is_running: bool
    task_count: int
    active_sync_count: int
    max_concurrent: int
    tasks: List[Dict[str, Any]] = Field(default_factory=list)


# ============================================================
# Webhook Schemas (Phase 2 - prepared for future)
# ============================================================

class ConfluenceWebhookPayloadSchema(BaseModel):
    """Webhook event payload from Confluence"""
    model_config = ConfigDict(extra="allow")

    event: str  # page_created | page_updated | page_removed | page_restored
    timestamp: str
    userAccountId: Optional[str] = None
    page: Optional[Dict[str, Any]] = None
    space: Optional[Dict[str, Any]] = None


class ConfluenceWebhookConfigSchema(BaseModel):
    """Webhook configuration"""
    webhook_id: Optional[str] = None
    connection_id: str
    callback_url: str
    secret: str
    events: List[str] = Field(default_factory=lambda: [
        "page_created", "page_updated", "page_removed"
    ])
    status: str = "active"
    created_at: Optional[str] = None


# ============================================================
# Error Response Schemas
# ============================================================

class ConfluenceErrorSchema(BaseModel):
    """Error response"""
    error: str
    message: str
    details: Optional[Dict[str, Any]] = None
    status_code: Optional[int] = None


# ============================================================
# Batch Operation Schemas
# ============================================================

class ConfluenceBatchSyncRequestSchema(BaseModel):
    """Request to batch sync multiple pages by their record IDs"""
    model_config = ConfigDict(extra="allow")

    page_record_ids: List[str] = Field(..., description="List of page record IDs from confluence_pages table")
    force: bool = Field(default=False, description="Force re-sync even if content unchanged")


class ConfluenceBatchSyncResultSchema(BaseModel):
    """Batch sync operation result"""
    total_pages: int = 0
    synced_pages: int = 0
    skipped_pages: int = 0
    failed_pages: int = 0
    created_documents: List[str] = Field(default_factory=list)
    updated_documents: List[str] = Field(default_factory=list)
    deleted_documents: List[str] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    success_rate: float = 0.0
    has_errors: bool = False
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


# Update forward references for self-referential schemas
ConfluencePageTreeNodeSchema.model_rebuild()
