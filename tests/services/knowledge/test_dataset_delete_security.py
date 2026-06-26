from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from knowledge_service.core.exceptions import PermissionDeniedError, ValidationFailedError
from knowledge_service.services.knowledge.dataset_service import DatasetService

from src.core.auth.password import hash_password
from src.core.auth.user_resolver import UserContext


@pytest.fixture
def mock_service() -> DatasetService:
    """Create a lightweight service instance for dataset deletion tests."""
    svc = object.__new__(DatasetService)
    svc.db = AsyncMock()
    svc._ks = AsyncMock()
    svc._ks.vector_store = AsyncMock()
    svc.require_dataset_access = AsyncMock(
        return_value={
            "dataset_id": "kb_test",
            "collection_name": "col_kb_test",
        }
    )
    return svc


@pytest.mark.asyncio
async def test_delete_dataset_requires_authenticated_user(mock_service: DatasetService) -> None:
    user = UserContext(user_id="anon:test", is_authenticated=False, roles=["guest"])

    with pytest.raises(PermissionDeniedError):
        await mock_service.delete_dataset(user, "kb_test", password="irrelevant")


@pytest.mark.asyncio
async def test_delete_dataset_rejects_invalid_password(mock_service: DatasetService) -> None:
    user = UserContext(user_id="u_test", tenant_id="t1", is_authenticated=True, roles=["user"])
    mock_service.db.get_user.return_value = {"password_hash": "Correct#123"}

    with pytest.raises(ValidationFailedError):
        await mock_service.delete_dataset(user, "kb_test", password="wrong-password")

    mock_service.db.delete_dataset.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_dataset_accepts_bcrypt_password_hash(mock_service: DatasetService) -> None:
    user = UserContext(user_id="u_test", tenant_id="tenant_a", is_authenticated=True, roles=["user"])
    mock_service.db.get_user.return_value = {"password_hash": hash_password("Correct#123")}
    mock_service.db.delete_dataset.return_value = True

    deleted = await mock_service.delete_dataset(
        user,
        "kb_test",
        password="Correct#123",
        reason="cleanup obsolete kb",
    )

    assert deleted is True
    mock_service.db.delete_dataset.assert_awaited_once_with(
        "kb_test",
        deleted_by="u_test",
        delete_reason="cleanup obsolete kb",
    )


@pytest.mark.asyncio
async def test_delete_dataset_soft_delete_and_audit(mock_service: DatasetService) -> None:
    user = UserContext(
        user_id="u_test", tenant_id="tenant_a", is_authenticated=True, roles=["user"]
    )
    mock_service.db.get_user.return_value = {"password_hash": "Correct#123"}
    mock_service.db.delete_dataset.return_value = True

    deleted = await mock_service.delete_dataset(
        user,
        "kb_test",
        password="Correct#123",
        reason="cleanup obsolete kb",
    )

    assert deleted is True
    mock_service._ks.vector_store.delete_collection.assert_awaited_once_with(
        collection_name="col_kb_test"
    )
    mock_service.db.delete_dataset.assert_awaited_once_with(
        "kb_test",
        deleted_by="u_test",
        delete_reason="cleanup obsolete kb",
    )
    mock_service.db.log_audit.assert_awaited_once()
