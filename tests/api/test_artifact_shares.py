"""Artifact share API + manager tests (product-convergence PC-03).

The manager is kind-generic; quiz shares freeze a payload snapshot with
answer keys. The gateway endpoints create/revoke shares and the public
quiz routes alias over the same rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from ai_gateway_core.sharing.artifact_share_manager import ArtifactShareManager
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.v1.artifact_shares import router as artifact_shares_router


class _FakeDB:
    """Minimal async DB double for the artifact share flow."""

    def __init__(self) -> None:
        self.shares: dict[str, dict] = {}
        self.quiz_rows: dict[str, dict] = {}
        self.attempts: list[dict] = []

    async def fetchrow(self, query: str, *args):  # noqa: ANN201
        if "FROM quizzes" in query:
            quiz_id = str(args[0])
            row = self.quiz_rows.get(quiz_id)
            return row or None
        if "FROM artifact_shares" in query:
            # Looked up by share_code (public reads) or by id (revoke).
            key = str(args[0])
            row = self.shares.get(key) or next(
                (s for s in self.shares.values() if s["share_code"] == key), None
            )
            if not row or not row["is_active"]:
                return None
            return row
        if "FROM quiz_attempts" in query and "LIMIT 1" in query:
            return self.attempts[0] if self.attempts else None
        return None

    async def fetch(self, query: str, *args):  # noqa: ANN201
        return []

    async def execute(self, query: str, *args):  # noqa: ANN201
        if query.lstrip().upper().startswith("INSERT INTO ARTIFACT_SHARES"):
            (
                _id, share_code, kind, title, payload, answer_keys, tenant_id,
                created_by, max_attempts, expires_at, require_name,
                time_limit_minutes, created_at,
            ) = args[:13]
            self.shares[str(_id)] = {
                "id": _id,
                "share_code": share_code,
                "kind": kind,
                "title": title,
                "payload": payload,
                "answer_keys": answer_keys,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "is_active": True,
                "max_attempts": max_attempts,
                "expires_at": expires_at,
                "require_name": require_name,
                "time_limit_minutes": time_limit_minutes,
                "attempt_count": 0,
                "created_at": created_at,
            }
        elif "UPDATE artifact_shares" in query:
            share_id = args[0]
            row = self.shares.get(str(share_id))
            if row is None:
                return "UPDATE 0"
            row["is_active"] = False
            return "UPDATE 1"
        return "OK"


@pytest.fixture
def fake_db() -> _FakeDB:
    return _FakeDB()


@pytest.mark.asyncio
async def test_manager_create_and_public_snapshot(fake_db: _FakeDB) -> None:
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz",
        title="Demo Quiz",
        payload={"quiz_id": "q1", "questions": [{"id": "a", "question_text": "Q?"}]},
        answer_keys=[{"id": "a", "correct_answer": ["A"]}],
        tenant_id="tenant-a",
        user_id="alex",
        max_attempts=2,
    )
    assert share["share_code"]
    assert share["kind"] == "quiz"

    public = await mgr.get_public_artifact(share["share_code"])
    assert public is not None
    assert public["title"] == "Demo Quiz"
    assert public["questions"][0]["id"] == "a"
    assert "answer_keys" not in public


@pytest.mark.asyncio
async def test_manager_expiry_and_revoke(fake_db: _FakeDB) -> None:
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz", title="T", payload={}, answer_keys=None,
        tenant_id="t", user_id="alex", expires_hours=1,
    )
    row = fake_db.shares[share["share_id"]]
    row["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert await mgr.get_public_artifact(share["share_code"]) is None

    # Re-activate, then revoke.
    row["expires_at"] = None
    assert await mgr.revoke_share(share["share_id"], "alex")
    assert await mgr.get_public_artifact(share["share_code"]) is None


def test_create_share_endpoint_rejects_unowned_quiz(fake_db: _FakeDB) -> None:
    app = FastAPI()
    app.include_router(artifact_shares_router)
    app.state.database = fake_db

    class _User:
        user_id = "alex"
        tenant_id = "tenant-a"

    async def _user_override():
        return _User()

    from src.api.v1.artifact_shares import get_user_context

    app.dependency_overrides[get_user_context] = _user_override
    client = TestClient(app)

    resp = client.post(
        "/artifact-shares",
        json={"kind": "quiz", "quiz_id": str(uuid.uuid4()), "require_name": True},
    )
    assert resp.status_code == 404

    resp = client.post(
        "/artifact-shares",
        json={"kind": "conversation", "quiz_id": None},
    )
    assert resp.status_code == 400
