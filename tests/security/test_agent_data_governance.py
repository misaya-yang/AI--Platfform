from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_user_context
from src.api.v1 import agents as agents_api
from src.core.auth.user_resolver import UserContext

AGENT_ID = str(uuid.uuid4())


def _user() -> UserContext:
    return UserContext(
        user_id="owner-a",
        tenant_id="tenant-a",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
        ip="127.0.0.1",
    )


class _Repository:
    def __init__(self) -> None:
        self.finished: bool | None = None

    async def list_agents(self, **_: Any) -> dict[str, Any]:
        return {"items": [], "next_cursor": None}

    async def prepare_agent_data_deletion(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "deletion_id": str(uuid.uuid4()),
            "tenant_id": kwargs["tenant_id"],
            "agent_id": kwargs["agent_id"],
            "scope": kwargs["scope"],
            "subject_user_id": kwargs["subject_user_id"],
            "status": "pending",
            "object_keys": ["tenant-a/opaque-object"],
            "deleted_counts": {},
            "error_code": None,
            "requested_by": kwargs["user_id"],
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }

    async def finish_agent_data_deletion(self, **kwargs: Any) -> dict[str, Any]:
        self.finished = kwargs["storage_cleanup_succeeded"]
        now = datetime.now(timezone.utc).isoformat()
        return {
            "deletion_id": kwargs["deletion_id"],
            "tenant_id": kwargs["tenant_id"],
            "agent_id": kwargs["agent_id"],
            "scope": "tenant",
            "subject_user_id": None,
            "status": "completed" if self.finished else "failed",
            "deleted_counts": {},
            "error_code": None if self.finished else "AGENT_STORAGE_CLEANUP_FAILED",
            "requested_by": kwargs["user_id"],
            "requested_at": now,
            "completed_at": now,
        }


class _FailingStorage:
    async def delete_file(self, _: str) -> bool:
        return False


def _client(repository: _Repository) -> TestClient:
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1")
    app.state.agent_repository = repository
    app.dependency_overrides[get_user_context] = _user
    return TestClient(app)


def test_management_flag_is_read_only_and_preserves_non_agent_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1")

    @app.get("/api/v1/assistant/health")
    async def assistant_health() -> dict[str, bool]:
        return {"ok": True}

    app.state.agent_repository = repository
    app.dependency_overrides[get_user_context] = _user
    monkeypatch.setenv("AGENT_STUDIO_MANAGEMENT_ENABLED", "false")
    client = TestClient(app)

    assert client.get("/api/v1/agents").status_code == 200
    denied = client.post(
        "/api/v1/agents",
        json={"name": "blocked", "description": "", "spec": {}},
    )
    assert denied.status_code == 503
    assert denied.json()["detail"]["code"] == "AGENT_STUDIO_MUTATIONS_DISABLED"
    assert client.get("/api/v1/assistant/health").json() == {"ok": True}


def test_storage_failure_seals_deletion_failed_without_exposing_object_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(agents_api, "get_file_storage", lambda: _FailingStorage())
    response = _client(repository).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions",
        json={"scope": "tenant", "idempotency_key": "delete-tenant-0001"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "AGENT_STORAGE_CLEANUP_FAILED"
    assert repository.finished is False
    assert "opaque-object" not in response.text


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"scope": "user", "idempotency_key": "delete-user-0001"}, "subject_user_id"),
        (
            {
                "scope": "tenant",
                "subject_user_id": "user-a",
                "idempotency_key": "delete-tenant-0001",
            },
            "subject_user_id",
        ),
    ],
)
def test_deletion_scope_subject_shape_is_closed(
    payload: dict[str, Any], expected_fragment: str
) -> None:
    response = _client(_Repository()).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions", json=payload
    )
    assert response.status_code == 422
    assert expected_fragment in response.text
