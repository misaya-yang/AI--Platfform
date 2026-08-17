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
        self.submitters: set[tuple[str, str]] = set()

    async def fetchrow(self, query: str, *args):  # noqa: ANN201
        query = query.replace("assistant.", "")
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

    async def fetch(self, query: str, *args):  # noqa: ANN201, ARG002
        return []

    async def execute(self, query: str, *args):  # noqa: ANN201
        query = query.replace("assistant.", "")
        if query.lstrip().upper().startswith("INSERT INTO ARTIFACT_SHARE_SUBMITTERS"):
            key = (str(args[0]), args[1])
            if key in self.submitters:
                return "INSERT 0 0"
            self.submitters.add(key)
            return "INSERT 0 1"
        if query.lstrip().upper().startswith("DELETE FROM ARTIFACT_SHARE_SUBMITTERS"):
            self.submitters.discard((str(args[0]), args[1]))
            return "DELETE 1"
        if query.lstrip().upper().startswith("INSERT INTO QUIZ_ATTEMPTS"):
            self.attempts.append({"args": args, "status": "completed"})
            return "OK"
        if query.lstrip().upper().startswith("UPDATE QUIZ_ATTEMPTS"):
            if self.attempts:
                self.attempts[-1]["status"] = "rejected"
            return "OK"
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
            if "attempt_count = attempt_count + 1" in query:
                # Mirrors the atomic reservation: only one row-version wins.
                if not row["is_active"]:
                    return "UPDATE 0"
                if (
                    row["max_attempts"] is not None
                    and row["attempt_count"] >= row["max_attempts"]
                ):
                    return "UPDATE 0"
                row["attempt_count"] += 1
                return "UPDATE 1"
            if len(args) == 3 and (
                row["created_by"] != args[1] or row["tenant_id"] != args[2]
            ):
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
    assert await mgr.revoke_share(share["share_id"], "alex", "t")
    assert await mgr.get_public_artifact(share["share_code"]) is None


@pytest.mark.asyncio
async def test_manager_submit_enforces_max_attempts(fake_db: _FakeDB) -> None:
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz",
        title="Capped",
        payload={"quiz_id": None},
        answer_keys=[],
        tenant_id="tenant-a",
        user_id="alex",
        max_attempts=1,
        require_name=False,
    )
    first = await mgr.submit_attempt(share["share_code"], answers={})
    assert first["correct_count"] == 0
    assert fake_db.shares[share["share_id"]]["attempt_count"] == 1

    with pytest.raises(ValueError, match="max attempts"):
        await mgr.submit_attempt(share["share_code"], answers={})


@pytest.mark.asyncio
async def test_reserve_attempt_slot_is_atomic_at_the_cap(fake_db: _FakeDB) -> None:
    """The conditional UPDATE must reject when the cap is already consumed."""
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz", title="T", payload={}, max_attempts=1, require_name=False,
    )
    row = fake_db.shares[share["share_id"]]
    row["attempt_count"] = 1  # consumed concurrently after the read-side check

    with pytest.raises(ValueError, match="max attempts"):
        await mgr._reserve_attempt_slot({"share_id": share["share_id"]})


@pytest.mark.asyncio
async def test_grade_quiz_marks_attempt_rejected_when_slot_lost(fake_db: _FakeDB) -> None:
    """An attempt that loses the slot race is kept but marked 'rejected'."""
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz",
        title="Raced",
        payload={"quiz_id": None},
        answer_keys=[],
        max_attempts=1,
        require_name=False,
    )
    fake_db.shares[share["share_id"]]["attempt_count"] = 1

    with pytest.raises(ValueError, match="max attempts"):
        await mgr._grade_quiz(
            {
                "share_id": share["share_id"],
                "answer_keys": [],
                "payload": {"quiz_id": None},
            },
            answers={},
            display_name=None,
            client_ip=None,
        )
    assert fake_db.attempts[-1]["status"] == "rejected"


@pytest.mark.asyncio
async def test_display_name_claim_is_atomic(
    fake_db: _FakeDB,
) -> None:
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz",
        title="Named",
        payload={"quiz_id": None},
        answer_keys=[],
        max_attempts=2,
        require_name=True,
    )

    await mgr.submit_attempt(share["share_code"], answers={}, display_name="Alex")
    with pytest.raises(ValueError, match="already submitted"):
        await mgr.submit_attempt(share["share_code"], answers={}, display_name="Alex")


@pytest.mark.asyncio
async def test_revoke_requires_matching_tenant(fake_db: _FakeDB) -> None:
    mgr = ArtifactShareManager(db=fake_db)
    share = await mgr.create_share(
        kind="quiz",
        title="Tenant scoped",
        payload={},
        tenant_id="tenant-a",
        user_id="alex",
    )

    assert not await mgr.revoke_share(share["share_id"], "alex", "tenant-b")
    assert await mgr.get_public_artifact(share["share_code"]) is not None
    assert await mgr.revoke_share(share["share_id"], "alex", "tenant-a")


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


def test_create_share_endpoint_returns_typed_contract(fake_db: _FakeDB) -> None:
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
    quiz_id = uuid.uuid4()
    fake_db.quiz_rows[str(quiz_id)] = {
        "id": quiz_id,
        "tenant_id": "tenant-a",
        "title": "Typed quiz",
        "description": "contract",
        "question_count": 0,
        "difficulty": "medium",
    }

    response = client.post(
        "/artifact-shares",
        json={"kind": "quiz", "quiz_id": str(quiz_id)},
    )

    assert response.status_code == 200
    assert response.json()["quiz_id"] == str(quiz_id)
    assert response.json()["quiz_title"] == "Typed quiz"


def test_create_share_endpoint_rejects_invalid_bounds(fake_db: _FakeDB) -> None:
    """expires_hours / max_attempts must be positive — schema-enforced."""
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

    for body in (
        {"kind": "quiz", "quiz_id": None, "max_attempts": 0},
        {"kind": "quiz", "quiz_id": None, "expires_hours": -1},
    ):
        resp = client.post("/artifact-shares", json=body)
        assert resp.status_code == 422, body


def test_artifact_share_endpoints_reject_malformed_uuids(fake_db: _FakeDB) -> None:
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

    create = client.post(
        "/artifact-shares",
        json={"kind": "quiz", "quiz_id": "not-a-uuid"},
    )
    assert create.status_code == 422
    assert client.delete("/artifact-shares/not-a-uuid").status_code == 422


def test_artifact_share_openapi_has_typed_success_responses() -> None:
    app = FastAPI()
    app.include_router(artifact_shares_router)
    paths = app.openapi()["paths"]

    create_schema = paths["/artifact-shares"]["post"]["responses"]["200"]["content"]
    revoke_schema = paths["/artifact-shares/{share_id}"]["delete"]["responses"]["200"][
        "content"
    ]
    assert create_schema["application/json"]["schema"]["$ref"].endswith(
        "/ArtifactShareCreateResponse"
    )
    assert revoke_schema["application/json"]["schema"]["$ref"].endswith(
        "/ArtifactShareRevokeResponse"
    )


def test_artifact_share_manager_uses_canonical_assistant_schema() -> None:
    """Gateway and Assistant use different search paths in production."""
    import inspect

    source = inspect.getsource(ArtifactShareManager)
    for table in ("artifact_shares", "artifact_share_submitters", "quiz_attempts"):
        assert f"assistant.{table}" in source


def test_revoke_share_endpoint_returns_503_without_database(fake_db: _FakeDB) -> None:
    """Regression: revoke must not AttributeError into a 500 when db is missing."""
    app = FastAPI()
    app.include_router(artifact_shares_router)
    app.state.database = None

    class _User:
        user_id = "alex"
        tenant_id = "tenant-a"

    async def _user_override():
        return _User()

    from src.api.v1.artifact_shares import get_user_context

    app.dependency_overrides[get_user_context] = _user_override
    client = TestClient(app)

    resp = client.delete(f"/artifact-shares/{uuid.uuid4()}")
    assert resp.status_code == 503
