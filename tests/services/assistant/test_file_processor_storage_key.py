from __future__ import annotations

import pytest
from assistant_service.auth import UserContext
from assistant_service.core.files.file_processor import FileProcessError, FileProcessor


def _user() -> UserContext:
    return UserContext(
        user_id="user-1",
        tenant_id="tenant-a",
        is_authenticated=True,
    )


def test_storage_key_accepts_tenant_prefixed_and_legacy_owner_paths() -> None:
    processor = FileProcessor()
    user = _user()
    assert (
        processor._get_storage_key("/uploads/tenant-a/user-1/abc_20260101.txt", user)
        == "uploads/tenant-a/user-1/abc_20260101.txt"
    )
    assert (
        processor._get_storage_key("/uploads/user-1/abc_20260101.txt", user)
        == "uploads/user-1/abc_20260101.txt"
    )


def test_storage_key_rejects_cross_tenant_and_cross_user_paths() -> None:
    processor = FileProcessor()
    user = _user()
    with pytest.raises(FileProcessError):
        processor._get_storage_key("/uploads/other-tenant/user-1/abc.txt", user)
    with pytest.raises(FileProcessError):
        processor._get_storage_key("/uploads/user-2/abc.txt", user)
