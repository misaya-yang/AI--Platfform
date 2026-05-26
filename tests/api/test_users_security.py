from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.deps import AuthContext
from src.api.v1.users import UserUpdate, update_user


class _RBAC:
    def require(self, _roles: list[str], permission: str) -> None:
        assert permission == "user:edit"


class _DB:
    enabled = True

    def __init__(self) -> None:
        self.updated = False

    async def get_user(self, user_id: str) -> dict:
        return {"user_id": user_id, "metadata": {}}

    async def update_user(self, _user_id: str, _updates: dict) -> None:
        self.updated = True


def _request(db: _DB) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                dispatcher=SimpleNamespace(rbac=_RBAC()),
                database=db,
            )
        )
    )


@pytest.mark.asyncio
async def test_non_admin_cannot_modify_user_service_access_policy():
    db = _DB()

    with pytest.raises(HTTPException) as exc:
        await update_user(
            user_id="target-user",
            body=UserUpdate(service_access_mode="allowlist", allowed_services=["imam"]),
            request=_request(db),
            auth=AuthContext(user_id="editor", roles=["user:edit"], is_authenticated=True),
        )

    assert exc.value.status_code == 403
    assert db.updated is False
