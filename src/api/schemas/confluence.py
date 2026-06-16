"""
Confluence Integration API Schemas.

Pydantic models for Confluence API requests and responses.
"""

from __future__ import annotations

from typing import Any

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
    polling_interval_minutes: int = Field(
        default=60, ge=5, le=1440, description="Polling interval in minutes"
    )


class ConfluenceConnectionUpdateSchema(BaseModel):
    """Update an existing Confluence connection"""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    email: str | None = None
    api_token: str | None = None
    sync_mode: str | None = None
    polling_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    status: str | None = None


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
    last_sync_at: str | None = None
    last_error: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ConfluenceConnectionTestResponseSchema(BaseModel):
    """Connection test result"""

    status: str  # success | error
    message: str
    spaces_available: bool | None = None
    status_code: int | None = None


# ============================================================
# Space Binding Schemas
# ============================================================


class ConfluenceSpaceBindingCreateSchema(BaseModel):
    """Bind a Confluence space to a dataset"""

    model_config = ConfigDict(extra="allow")

    dataset_id: str = Field(..., description="Target dataset ID")
    space_key: str = Field(..., description="Confluence space key (e.g., 'ENG')")
    root_page_id: str | None = Field(
        default=None, description="Root page ID to sync from (deprecated, use root_page_ids)"
    )
    root_page_ids: list[str] = Field(
        default_factory=list, description="List of root page IDs to sync (supports multi-select)"
    )
    include_patterns: list[str] = Field(
        default_factory=list, description="Title patterns to include"
    )
    exclude_patterns: list[str] = Field(
        default_factory=list, description="Title patterns to exclude"
    )
    max_depth: int = Field(default=10, ge=1, le=100, description="Maximum page hierarchy depth")
    include_attachments: bool = Field(default=False, description="Include page attachments")
    include_comments: bool = Field(default=False, description="Include page comments")
    sync_images: bool = Field(
        default=True, description="Sync and embed images from pages (requires multimodal embedding)"
    )
    image_max_size_bytes: int = Field(
        default=3 * 1024 * 1024,
        ge=1024,
        le=10 * 1024 * 1024,
        description="Maximum image size in bytes (default 3MB)",
    )


class ConfluenceSpaceBindingUpdateSchema(BaseModel):
    """Update a space binding (including sync mode configuration)"""

    model_config = ConfigDict(extra="allow")

    root_page_id: str | None = Field(
        default=None, description="Root page ID (deprecated, use root_page_ids)"
    )
    root_page_ids: list[str] | None = Field(
        default=None, description="List of root page IDs (supports multi-select)"
    )
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    max_depth: int | None = Field(default=None, ge=1, le=100)
    include_attachments: bool | None = None
    include_comments: bool | None = None
    # Sync mode configuration (binding-level)
    sync_mode: str | None = Field(default=None, description="Sync mode: manual | polling")
    polling_interval_minutes: int | None = Field(
        default=None, ge=5, le=1440, description="Polling interval in minutes"
    )
    sync_enabled: bool | None = Field(default=None, description="Enable/disable auto sync")
    # Image sync configuration
    sync_images: bool | None = Field(default=None, description="Sync and embed images from pages")
    image_max_size_bytes: int | None = Field(
        default=None, ge=1024, le=10 * 1024 * 1024, description="Maximum image size in bytes"
    )


class ConfluenceSpaceBindingResponseSchema(BaseModel):
    """Space binding response"""

    binding_id: str
    connection_id: str
    tenant_id: str | None = None
    dataset_id: str
    space_key: str
    space_id: str | None = None
    space_name: str | None = None
    root_page_id: str | None = None
    root_page_ids: list[str] = Field(default_factory=list)
    root_page_title: str | None = None
    root_page_titles: list[str] = Field(default_factory=list)
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    max_depth: int
    include_attachments: bool
    include_comments: bool
    sync_images: bool = True
    image_max_size_bytes: int = 3 * 1024 * 1024
    status: str
    last_sync_at: str | None = None
    synced_page_count: int = 0
    total_page_count: int = 0
    last_error: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # 创建绑定时自动触发的首次同步任务 ID
    initial_sync_task_id: str | None = None
    # Sync mode configuration (binding-level)
    sync_mode: str = "manual"
    polling_interval_minutes: int = 60
    last_incremental_sync_at: str | None = None
    sync_enabled: bool = True
    next_sync_at: str | None = None


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
    homepage_id: str | None = None
    description: str | None = None


class ConfluenceSpaceListResponseSchema(BaseModel):
    """List of discovered spaces"""

    spaces: list[ConfluenceSpaceInfoSchema] = Field(default_factory=list)
    total: int = 0


# ============================================================
# Page Tree Schemas (for folder/page hierarchy selection)
# ============================================================


class ConfluencePageTreeNodeSchema(BaseModel):
    """Page tree node for hierarchy display"""

    page_id: str
    title: str
    parent_id: str | None = None
    has_children: bool = False
    children: list[ConfluencePageTreeNodeSchema] = Field(default_factory=list)
    depth: int = 0
    web_url: str | None = None


class ConfluencePageTreeResponseSchema(BaseModel):
    """Page tree response for space hierarchy"""

    space_key: str
    space_name: str
    root_pages: list[ConfluencePageTreeNodeSchema] = Field(default_factory=list)
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
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class ConfluenceSpaceImportSchema(BaseModel):
    """Trigger a full space import"""

    model_config = ConfigDict(extra="allow")

    binding_id: str = Field(..., description="Space binding ID")
    force_full_sync: bool = Field(
        default=False, description="Force full sync ignoring change detection"
    )


class ConfluenceImportResultSchema(BaseModel):
    """Import operation result"""

    document_id: str | None = None
    page_id: str | None = None
    title: str | None = None
    status: str  # created | updated | skipped | failed
    message: str | None = None
    web_url: str | None = None


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
    last_sync_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


class ConfluenceSyncTaskSchema(BaseModel):
    """Sync task details"""

    task_id: str
    binding_id: str | None = None
    page_id: str | None = None
    task_type: str
    priority: int
    status: str
    retry_count: int
    progress: float
    total_items: int
    processed_items: int
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None


class ConfluenceSyncTriggerSchema(BaseModel):
    """Trigger a sync operation"""

    model_config = ConfigDict(extra="allow")

    force: bool = Field(default=False, description="Force full sync")
    page_ids: list[str] | None = Field(default=None, description="Specific page IDs to sync")


# ============================================================
# Page Record Schemas
# ============================================================


class ConfluencePageRecordSchema(BaseModel):
    """Synced page record"""

    id: str
    binding_id: str
    document_id: str | None = None
    page_id: str
    space_key: str
    title: str
    version: int
    content_hash: str | None = None
    parent_page_id: str | None = None
    depth: int = 0
    status: str
    # effective_status: 计算后的有效状态
    # 当 status='synced' 但文档不存在或无 segment 时，返回 'needs_resync'
    effective_status: str | None = None
    # 关联文档的处理状态
    document_status: str | None = None
    document_progress: int | None = None
    last_synced_at: str | None = None
    confluence_updated_at: str | None = None
    error: str | None = None
    labels: list[str] = Field(default_factory=list)
    web_url: str | None = None
    author: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Page-level sync configuration (overrides binding defaults)
    sync_mode: str | None = None  # NULL=inherit, manual, polling
    polling_interval_minutes: int | None = None
    sync_enabled: bool = True
    next_sync_at: str | None = None
    sync_priority: int = 0


class ConfluencePageSyncConfigUpdateSchema(BaseModel):
    """Update page-level sync configuration"""

    model_config = ConfigDict(extra="allow")

    sync_mode: str | None = Field(
        default=None, description="Sync mode: NULL(inherit) | manual | polling"
    )
    polling_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    sync_enabled: bool | None = None
    sync_priority: int | None = Field(default=None, ge=0, le=100)


class ConfluencePageListResponseSchema(BaseModel):
    """List of page records"""

    pages: list[ConfluencePageRecordSchema] = Field(default_factory=list)
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
    tasks: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================
# Webhook Schemas (Phase 2 - prepared for future)
# ============================================================


class ConfluenceWebhookPayloadSchema(BaseModel):
    """Webhook event payload from Confluence"""

    model_config = ConfigDict(extra="allow")

    event: str  # page_created | page_updated | page_removed | page_restored
    timestamp: str
    userAccountId: str | None = None
    page: dict[str, Any] | None = None
    space: dict[str, Any] | None = None


class ConfluenceWebhookConfigSchema(BaseModel):
    """Webhook configuration"""

    webhook_id: str | None = None
    connection_id: str
    callback_url: str
    secret: str
    events: list[str] = Field(
        default_factory=lambda: ["page_created", "page_updated", "page_removed"]
    )
    status: str = "active"
    created_at: str | None = None


# ============================================================
# Error Response Schemas
# ============================================================


class ConfluenceErrorSchema(BaseModel):
    """Error response"""

    error: str
    message: str
    details: dict[str, Any] | None = None
    status_code: int | None = None


# ============================================================
# Batch Operation Schemas
# ============================================================


class ConfluenceBatchSyncRequestSchema(BaseModel):
    """Request to batch sync multiple pages by their record IDs"""

    model_config = ConfigDict(extra="allow")

    page_record_ids: list[str] = Field(
        ..., description="List of page record IDs from confluence_pages table"
    )
    force: bool = Field(default=False, description="Force re-sync even if content unchanged")


class ConfluenceBatchSyncResultSchema(BaseModel):
    """Batch sync operation result"""

    total_pages: int = 0
    synced_pages: int = 0
    skipped_pages: int = 0
    failed_pages: int = 0
    created_documents: list[str] = Field(default_factory=list)
    updated_documents: list[str] = Field(default_factory=list)
    deleted_documents: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    success_rate: float = 0.0
    has_errors: bool = False
    started_at: str | None = None
    completed_at: str | None = None


class ConfluenceRemovePagesRequestSchema(BaseModel):
    """Request to remove pages from confluence_pages table"""

    model_config = ConfigDict(extra="allow")

    page_record_ids: list[str] = Field(..., description="List of page record IDs to remove")
    delete_documents: bool = Field(
        default=True, description="Also delete corresponding documents from knowledge base"
    )


class ConfluenceRemovePagesResultSchema(BaseModel):
    """Remove pages operation result"""

    removed: int = Field(default=0, description="Number of page records removed")
    documents_deleted: int = Field(
        default=0, description="Number of documents deleted from knowledge base"
    )
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description="List of errors encountered"
    )


# Update forward references for self-referential schemas
ConfluencePageTreeNodeSchema.model_rebuild()
