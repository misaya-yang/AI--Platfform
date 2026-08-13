from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from knowledge_service.services.knowledge.confluence.sync_service import ConfluenceSyncService

from src.core.auth.user_resolver import UserContext


@pytest.mark.asyncio
async def test_remove_pages_deletes_linked_document_and_counts_it() -> None:
    binding = {
        "binding_id": "binding-1",
        "dataset_id": "dataset-1",
        "tenant_id": "tenant-1",
        "owner_id": "user-1",
        "created_by": "user-1",
    }
    database = MagicMock()
    database.get_confluence_page = AsyncMock(
        return_value={
            "id": "page-record-1",
            "binding_id": "binding-1",
            "document_id": "document-1",
        }
    )
    database.get_confluence_binding = AsyncMock(return_value=binding)
    database.delete_confluence_page = AsyncMock(return_value=True)
    knowledge_service = MagicMock()
    knowledge_service.delete_document = AsyncMock()
    service = ConfluenceSyncService(
        settings=SimpleNamespace(confluence=SimpleNamespace(client_cache_ttl_seconds=300)),
        database=database,
        knowledge_service=knowledge_service,
    )
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
    )

    result = await service.remove_pages(["page-record-1"], user, delete_documents=True)

    assert result == {"removed": 1, "documents_deleted": 1, "errors": []}
    knowledge_service.delete_document.assert_awaited_once_with(
        user,
        "dataset-1",
        "document-1",
    )
    database.delete_confluence_page.assert_awaited_once_with("page-record-1")
