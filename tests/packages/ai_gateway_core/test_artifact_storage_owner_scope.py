from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from ai_gateway_core.storage.artifact_storage import ArtifactInfo, ArtifactStorageService


def _artifact(*, owner_scope: str | None, tenant_id: str, user_id: str) -> ArtifactInfo:
    return ArtifactInfo(
        artifact_id="artifact-1",
        session_id="session-1",
        tenant_id=tenant_id,
        user_id=user_id,
        type="image",
        format="png",
        title="Image",
        filename="image.png",
        storage_key="artifact/image.png",
        owner_scope=owner_scope,
    )


@pytest.mark.asyncio
async def test_variant_url_legacy_null_owner_requires_matching_tenant_and_user() -> None:
    service = object.__new__(ArtifactStorageService)
    service.find_variant = AsyncMock(
        return_value=_artifact(owner_scope=None, tenant_id="tenant-1", user_id="user-1")
    )
    service.get_presigned_download_url = AsyncMock(return_value="https://storage.test/image")

    denied_without_fallback = await service.get_presigned_download_url_for_variant(
        "artifact-1",
        "raw",
        owner_scope="opaque-owner",
    )
    denied_for_other_user = await service.get_presigned_download_url_for_variant(
        "artifact-1",
        "raw",
        owner_scope="opaque-owner",
        tenant_id="tenant-1",
        user_id="other-user",
    )
    allowed = await service.get_presigned_download_url_for_variant(
        "artifact-1",
        "raw",
        owner_scope="opaque-owner",
        tenant_id="tenant-1",
        user_id="user-1",
    )

    assert denied_without_fallback == (None, None)
    assert denied_for_other_user == (None, None)
    assert allowed == ("https://storage.test/image", "raw")


@pytest.mark.asyncio
async def test_variant_url_non_null_owner_scope_must_match_exactly() -> None:
    service = object.__new__(ArtifactStorageService)
    service.find_variant = AsyncMock(
        return_value=_artifact(
            owner_scope="owner-a",
            tenant_id="tenant-1",
            user_id="user-1",
        )
    )
    service.get_presigned_download_url = AsyncMock(return_value="https://storage.test/image")

    denied = await service.get_presigned_download_url_for_variant(
        "artifact-1",
        "raw",
        owner_scope="owner-b",
        tenant_id="tenant-1",
        user_id="user-1",
    )

    assert denied == (None, None)
    service.get_presigned_download_url.assert_not_awaited()
