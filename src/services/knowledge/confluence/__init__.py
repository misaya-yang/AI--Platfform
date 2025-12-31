"""
Confluence Integration Module for Agent Gateway Knowledge Base.

This module provides integration with Atlassian Confluence for importing
and synchronizing documentation into the knowledge base.

Features:
- URL-based single page import
- Space-wide batch import
- Manual and scheduled synchronization
- Content parsing (Storage Format to plain text)

Phase 1 (Current):
- URL import
- Space import
- Manual sync
- Polling sync

Phase 2 (Future):
- Webhook real-time sync
- Encrypted API token storage
"""

from .client import ConfluenceClient, ConfluenceAPIError
from .models import (
    ConfluenceCredentials,
    ConfluencePage,
    ConfluenceSpace,
    SyncResult,
    ConfluenceConnection,
    ConfluenceSpaceBinding,
    ConfluencePageRecord,
    ConfluenceSyncTask,
)
from .parser import parse_storage_format, extract_plain_text, extract_markdown
from .scheduler import ConfluenceScheduler, SchedulerManager

__all__ = [
    # Client
    "ConfluenceClient",
    "ConfluenceAPIError",
    # Models
    "ConfluenceCredentials",
    "ConfluencePage",
    "ConfluenceSpace",
    "SyncResult",
    "ConfluenceConnection",
    "ConfluenceSpaceBinding",
    "ConfluencePageRecord",
    "ConfluenceSyncTask",
    # Parser
    "parse_storage_format",
    "extract_plain_text",
    "extract_markdown",
    # Scheduler
    "ConfluenceScheduler",
    "SchedulerManager",
]
