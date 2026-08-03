from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.schemas.assistant import AssistantChatRequest
from src.api.v1.assistant import _check_model_permission, _user_can_access_model
from src.core.auth.user_resolver import UserContext


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hello", "kb_dataset_ids": [f"dataset-{i}" for i in range(9)]},
        {"message": "hello", "kb_dataset_ids": ["dataset-a", "dataset-a"]},
        {"message": "hello", "kb_dataset_ids": ["x" * 129]},
        {"message": "hello", "kb_include_images": True},
    ],
)
def test_assistant_request_rejects_unbounded_or_multimodal_kb_scope(payload) -> None:
    with pytest.raises(ValidationError):
        AssistantChatRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("tier", "roles", "access_level", "expected"),
    [
        ("normal", [], "public", True),
        ("normal", [], "premium", False),
        ("premium", [], "premium", True),
        ("normal", [], "admin", False),
        ("admin", [], "admin", True),
        ("normal", ["admin"], "admin", True),
    ],
)
def test_user_model_access_preserves_known_access_level_rules(
    tier: str,
    roles: list[str],
    access_level: str,
    expected: bool,
) -> None:
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier=tier,
        roles=roles,
        is_authenticated=True,
    )

    assert _user_can_access_model(user, access_level) is expected


@pytest.mark.parametrize("tier", ["normal", "admin"])
def test_user_model_access_rejects_unknown_access_level_for_every_tier(tier: str) -> None:
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier=tier,
        is_authenticated=True,
    )

    assert _user_can_access_model(user, "corrupt") is False


@pytest.mark.asyncio
async def test_model_permission_rejects_dirty_database_access_level() -> None:
    user = UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier="admin",
        is_authenticated=True,
    )
    model_meta = SimpleNamespace()

    async def get_access_level(_tenant_id: str, _model_id: str) -> str:
        return "corrupt"

    model_meta.get_access_level = get_access_level

    with pytest.raises(HTTPException) as exc_info:
        await _check_model_permission(user, "model-1", model_meta)

    assert exc_info.value.status_code == 403
