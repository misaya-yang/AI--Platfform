"""Gateway quiz compatibility API regression tests."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.v1.quiz import get_user_context, router


class _QuizDB:
    def __init__(self) -> None:
        self.quiz_id = uuid.uuid4()
        self.question_id = uuid.uuid4()
        self.deleted = False
        self.attempts: list[dict] = []

    async def fetchrow(self, query: str, *args):  # noqa: ANN201
        if "count(*) AS cnt" in query:
            return {"cnt": len(self.attempts)}
        if "SELECT created_by FROM quizzes" in query:
            if args[0] == self.quiz_id and args[1] == "tenant-a":
                return {"created_by": "alex"}
            return None
        if "SELECT * FROM quizzes" in query:
            if args[0] != self.quiz_id or args[1] != "tenant-a" or self.deleted:
                return None
            return {
                "id": self.quiz_id,
                "title": "Shared boundary",
                "description": "Gateway-safe quiz",
                "topic": "architecture",
                "difficulty": "medium",
                "question_count": 1,
                "status": "ready",
                "created_at": datetime.now(timezone.utc),
            }
        return None

    async def fetch(self, query: str, *_args):  # noqa: ANN201
        if "FROM quiz_questions" in query:
            return [
                {
                    "id": self.question_id,
                    "question_num": 1,
                    "question_type": "mc_single",
                    "question_text": "Which package owns shared primitives?",
                    "options": [{"label": "A", "text": "ai_gateway_core"}],
                    "correct_answer": ["A"],
                    "explanation": "The gateway and assistant both import the core package.",
                }
            ]
        if "FROM quiz_attempts" in query:
            return self.attempts
        return []

    async def execute(self, query: str, *args):  # noqa: ANN201
        if query.lstrip().startswith("INSERT INTO quiz_attempts"):
            self.attempts.append(
                {
                    "id": args[0],
                    "user_id": args[2],
                    "display_name": None,
                    "total_score": args[4],
                    "correct_count": args[5],
                    "total_count": args[6],
                    "started_at": args[7],
                    "completed_at": args[8],
                    "status": args[9],
                    "answers": json.loads(args[3]),
                }
            )
            return "INSERT 0 1"
        if query.lstrip().startswith("DELETE FROM quizzes"):
            if args == (self.quiz_id, "tenant-a", "alex"):
                self.deleted = True
                return "DELETE 1"
            return "DELETE 0"
        return "OK"


def _client(db: _QuizDB) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.database = db

    class _User:
        user_id = "alex"
        tenant_id = "tenant-a"

    async def _user_override():
        return _User()

    app.dependency_overrides[get_user_context] = _user_override
    return TestClient(app)


def test_quiz_read_submit_attempts_and_delete_stay_gateway_local() -> None:
    db = _QuizDB()
    client = _client(db)

    quiz = client.get(f"/assistant/quiz/{db.quiz_id}")
    assert quiz.status_code == 200
    assert quiz.json()["questions"][0]["options"][0]["label"] == "A"
    assert "correct_answer" not in quiz.json()["questions"][0]

    submission = client.post(
        f"/assistant/quiz/{db.quiz_id}/submit",
        json={"answers": {str(db.question_id): "A"}},
    )
    assert submission.status_code == 200
    assert submission.json()["correct_count"] == 1

    attempts = client.get(f"/assistant/quiz/{db.quiz_id}/attempts")
    assert attempts.status_code == 200
    assert attempts.json()["total"] == 1
    assert len(attempts.json()["attempts"]) == 1

    deleted = client.delete(f"/assistant/quiz/{db.quiz_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
