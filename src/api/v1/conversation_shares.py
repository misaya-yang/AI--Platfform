"""Conversation Share API — share assistant conversations with artifacts as public snapshots."""

from __future__ import annotations

import json
import secrets
import string
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from ai_gateway_core.logging import get_logger
from ai_gateway_core.quiz import QuizGrader
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ...core.client_ip import get_client_ip_from_request
from ..deps import enforce_rate_limit, get_user_context
from ._artifact_headers import attachment_content_disposition

logger = get_logger(__name__)
router = APIRouter(prefix="/assistant", tags=["conversation-shares"])

# ── Models ───────────────────────────────────────────────────────────


class CreateShareRequest(BaseModel):
    expires_days: int | None = Field(None, ge=1, le=365)
    include_artifacts: bool = True


class ShareResponse(BaseModel):
    share_code: str
    share_url: str
    title: str | None
    message_count: int
    artifact_count: int
    created_at: str
    expires_at: str | None


# ── Helpers ──────────────────────────────────────────────────────────


def _generate_share_code(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _get_db(request: Request):
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    return db


def _get_artifact_storage(request: Request):
    return getattr(request.app.state, "artifact_storage", None)


async def _collect_quiz_payloads(
    db,
    messages: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Freeze quiz content referenced by ``metadata.quiz_id`` on assistant messages.

    Returns a ``(public_quizzes, answer_keys)`` tuple:

    * ``public_quizzes`` — ``quiz_id → quiz payload`` safe to expose to anonymous
      viewers (questions + options, no answers). Embedded onto the snapshot
      messages so the share page can render the quiz even if the original quiz
      row is later deleted.
    * ``answer_keys`` — ``quiz_id → {questions: [...]}`` containing
      ``correct_answer`` + ``explanation``. Stored separately in the snapshot;
      **never returned by the public GET**. Used purely for grading anon
      submissions server-side.
    """
    quiz_ids: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        meta = msg.get("metadata")
        if not isinstance(meta, dict):
            continue
        qid = meta.get("quiz_id")
        if qid and isinstance(qid, str) and qid not in seen:
            seen.add(qid)
            quiz_ids.append(qid)

    public_quizzes: dict[str, dict[str, Any]] = {}
    answer_keys: dict[str, dict[str, Any]] = {}

    for quiz_id in quiz_ids:
        try:
            quiz_uuid = uuid.UUID(quiz_id)
        except (ValueError, TypeError):
            continue
        quiz_row = await db.fetchrow(
            "SELECT id, title, description, topic, difficulty, question_count FROM quizzes WHERE id = $1",
            quiz_uuid,
        )
        if not quiz_row:
            continue
        q_rows = await db.fetch(
            "SELECT id, question_num, question_type, question_text, options, correct_answer, explanation "
            "FROM quiz_questions WHERE quiz_id = $1 ORDER BY question_num",
            quiz_uuid,
        )
        if not q_rows:
            continue

        public_questions: list[dict[str, Any]] = []
        grading_questions: list[dict[str, Any]] = []
        for qr in q_rows:
            options = qr["options"]
            if isinstance(options, str):
                try:
                    options = json.loads(options)
                except (ValueError, TypeError):
                    options = []
            correct = qr["correct_answer"]
            if isinstance(correct, str):
                try:
                    correct = json.loads(correct)
                except (ValueError, TypeError):
                    correct = []
            qid_str = str(qr["id"])
            public_questions.append(
                {
                    "id": qid_str,
                    "question_num": qr["question_num"],
                    "question_type": qr["question_type"],
                    "question_text": qr["question_text"],
                    "options": options or [],
                }
            )
            grading_questions.append(
                {
                    "id": qid_str,
                    "question_num": qr["question_num"],
                    "question_type": qr["question_type"],
                    "correct_answer": correct,
                    "explanation": qr["explanation"] or "",
                }
            )

        public_quizzes[quiz_id] = {
            "quiz_id": quiz_id,
            "title": quiz_row["title"],
            "description": quiz_row["description"],
            "topic": quiz_row["topic"],
            "difficulty": quiz_row["difficulty"],
            "question_count": quiz_row["question_count"],
            "questions": public_questions,
        }
        answer_keys[quiz_id] = {"questions": grading_questions}

    return public_quizzes, answer_keys


def _strip_snapshot_for_public(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``snapshot`` safe for anonymous GET.

    Specifically removes the ``quiz_answer_keys`` field which contains grading
    secrets. The ``messages[].quiz_data`` embedded at snapshot time has already
    been stripped of answers, so it is safe to return as-is.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    public = dict(snapshot)
    public.pop("quiz_answer_keys", None)
    return public


# ── Create Share ─────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/share")
async def create_share(
    session_id: str,
    body: CreateShareRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Create a public share link for a conversation with artifacts."""
    db = _get_db(request)

    session = await db.fetchrow(
        "SELECT session_id, history, metadata, user_id, tenant_id "
        "FROM assistant.sessions WHERE session_id = $1",
        session_id,
    )
    if not session:
        raise HTTPException(404, "Session not found")
    if not session["user_id"] or session["user_id"] != user.user_id:
        raise HTTPException(403, "Not your session")

    history = (
        session["history"]
        if isinstance(session["history"], (list, dict))
        else json.loads(session["history"])
    )
    if not history:
        raise HTTPException(400, "Session has no messages")

    artifacts_data = []
    if body.include_artifacts:
        rows = await db.fetch(
            "SELECT artifact_id, type, format, title, filename, size_bytes, "
            "mime_type, source FROM assistant.artifacts WHERE session_id = $1",
            session_id,
        )
        artifacts_data = [dict(r) for r in rows]

    raw_meta = session["metadata"]
    if isinstance(raw_meta, dict):
        meta = raw_meta
    elif isinstance(raw_meta, str):
        meta = json.loads(raw_meta)
    else:
        meta = {}
    title = meta.get("title", "")
    model_id = None
    for msg in reversed(history):
        if isinstance(msg, dict) and msg.get("metadata", {}).get("model_id"):
            model_id = msg["metadata"]["model_id"]
            break

    # Freeze quiz content so the share page can render quizzes even if the
    # underlying quiz rows are later edited or deleted. Public quiz payload is
    # attached to the owning assistant message; grading answer keys are kept
    # separately on the snapshot root and stripped before public GET.
    public_quizzes, answer_keys = await _collect_quiz_payloads(db, history)
    if public_quizzes:
        for msg in history:
            if not isinstance(msg, dict):
                continue
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else None
            qid = meta.get("quiz_id") if meta else None
            if qid and qid in public_quizzes:
                msg["quiz_data"] = public_quizzes[qid]

    snapshot: dict[str, Any] = {
        "messages": history,
        "artifacts": artifacts_data,
        "model_id": model_id,
        "shared_at": datetime.now(timezone.utc).isoformat(),
    }
    if answer_keys:
        # Grading-only; never returned by the public GET.
        snapshot["quiz_answer_keys"] = answer_keys

    share_code = _generate_share_code()
    for _ in range(5):
        existing = await db.fetchrow(
            "SELECT id FROM conversation_shares WHERE share_code = $1", share_code
        )
        if not existing:
            break
        share_code = _generate_share_code()
    else:
        raise HTTPException(409, "Could not generate unique share code, try again")
        share_code = _generate_share_code()

    expires_at = None
    if body.expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_days)

    await db.execute(
        """
        INSERT INTO conversation_shares
            (share_code, session_id, user_id, tenant_id, title, snapshot, message_count, artifact_count, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
        """,
        share_code,
        session_id,
        user.user_id,
        user.tenant_id or "",
        title or f"Conversation ({len(history)} messages)",
        json.dumps(snapshot, ensure_ascii=False, default=str),
        len(history),
        len(artifacts_data),
        expires_at,
    )

    share_url = f"/share/{share_code}"
    logger.info(
        f"Created share {share_code} for session {session_id} ({len(history)} msgs, {len(artifacts_data)} artifacts)"
    )

    return ShareResponse(
        share_code=share_code,
        share_url=share_url,
        title=title,
        message_count=len(history),
        artifact_count=len(artifacts_data),
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
    )


# ── Public: Get Shared Conversation ──────────────────────────────────


@router.get("/shares/{share_code}")
async def get_share(share_code: str, request: Request):
    """Public endpoint — no auth required. Returns the shared conversation snapshot."""
    db = _get_db(request)
    row = await db.fetchrow(
        "SELECT * FROM conversation_shares WHERE share_code = $1 AND is_active = TRUE",
        share_code,
    )
    if not row:
        raise HTTPException(404, "Share not found or expired")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(410, "Share has expired")

    with suppress(Exception):
        await db.execute(
            "UPDATE conversation_shares SET view_count = view_count + 1 WHERE share_code = $1",
            share_code,
        )

    snapshot = row["snapshot"] if isinstance(row["snapshot"], dict) else json.loads(row["snapshot"])
    snapshot = _strip_snapshot_for_public(snapshot)

    return {
        "share_code": share_code,
        "title": row["title"],
        "snapshot": snapshot,
        "message_count": row["message_count"],
        "artifact_count": row["artifact_count"],
        "view_count": (row["view_count"] or 0) + 1,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


# ── Public: Submit Anonymous Quiz Attempt ────────────────────────────


class SharedQuizSubmitRequest(BaseModel):
    answers: dict[str, str] = Field(
        ...,
        description="question_id → selected option label (or free text for short_answer)",
    )


def _resolve_anon_id(request: Request) -> str:
    """Return the middleware-validated anonymous identity for a share viewer.

    ``StreamingAnonymousMiddleware`` validates or generates the stable ID and
    stores it on request state. Direct route invocations without that
    middleware retain a server-derived client-IP fallback; raw client-provided
    anonymous cookies and headers must not control quiz-attempt ownership.
    """
    anon_id = getattr(getattr(request, "state", None), "anonymous_id", None)
    if anon_id:
        return str(anon_id)[:128]
    return get_client_ip_from_request(request)[:128] or "anonymous"


@router.post("/shares/{share_code}/quiz/{quiz_id}/submit")
async def submit_shared_quiz(
    share_code: str,
    quiz_id: str,
    body: SharedQuizSubmitRequest,
    request: Request,
):
    """Grade an anonymous viewer's answers against the frozen quiz snapshot.

    * No auth required. Rate-limited by IP via ``enforce_rate_limit``.
    * Keyed by ``(share_code, anon_id, quiz_id)`` using the trusted anonymous
      middleware identity — one attempt per viewer per quiz; resubmits return
      the cached result rather than re-grading.
    * Grades against the **frozen** answer key embedded in the share's
      snapshot, so even if the original quiz row is deleted the share still
      functions.
    """
    await enforce_rate_limit(request, user=None, operation="quiz_submit_public")
    db = _get_db(request)

    row = await db.fetchrow(
        "SELECT snapshot, expires_at, is_active FROM conversation_shares WHERE share_code = $1",
        share_code,
    )
    if not row or not row["is_active"]:
        raise HTTPException(404, "Share not found")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(410, "Share has expired")

    snapshot = row["snapshot"] if isinstance(row["snapshot"], dict) else json.loads(row["snapshot"])
    answer_keys = snapshot.get("quiz_answer_keys") if isinstance(snapshot, dict) else None
    if not isinstance(answer_keys, dict) or quiz_id not in answer_keys:
        raise HTTPException(404, "Quiz not found in this share")

    anon_id = _resolve_anon_id(request)

    # Replay cached attempt if this anon viewer already submitted.
    try:
        quiz_uuid = uuid.UUID(quiz_id)
    except (ValueError, TypeError):
        raise HTTPException(404, "Quiz not found in this share")
    prior = await db.fetchrow(
        "SELECT result FROM conversation_share_quiz_attempts "
        "WHERE share_code = $1 AND anon_id = $2 AND quiz_id = $3",
        share_code,
        anon_id,
        quiz_uuid,
    )
    if prior:
        cached = (
            prior["result"] if isinstance(prior["result"], dict) else json.loads(prior["result"])
        )
        cached["cached"] = True
        return cached

    grader = QuizGrader()
    grading_questions = answer_keys[quiz_id].get("questions", [])
    result = grader.grade(grading_questions, body.answers)

    attempt_id = str(uuid.uuid4())
    result_payload: dict[str, Any] = {"attempt_id": attempt_id, **result}

    try:
        await db.execute(
            """
            INSERT INTO conversation_share_quiz_attempts
                (id, share_code, anon_id, quiz_id, result)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            uuid.UUID(attempt_id),
            share_code,
            anon_id,
            quiz_uuid,
            json.dumps(result_payload, default=str),
        )
    except Exception as e:
        # Likely a race — another parallel submit already inserted. Replay.
        logger.warning(f"Insert share quiz attempt failed ({e!r}); re-reading cached result")
        replay = await db.fetchrow(
            "SELECT result FROM conversation_share_quiz_attempts "
            "WHERE share_code = $1 AND anon_id = $2 AND quiz_id = $3",
            share_code,
            anon_id,
            quiz_uuid,
        )
        if replay:
            cached = (
                replay["result"]
                if isinstance(replay["result"], dict)
                else json.loads(replay["result"])
            )
            cached["cached"] = True
            return cached
        raise HTTPException(500, "Failed to record attempt")

    return result_payload


# ── Public: Download Shared Artifact ─────────────────────────────────


@router.get("/shares/{share_code}/artifact/{artifact_id}")
async def download_shared_artifact(share_code: str, artifact_id: str, request: Request):
    """Public endpoint — download an artifact from a shared conversation."""
    db = _get_db(request)
    row = await db.fetchrow(
        "SELECT snapshot, expires_at, is_active FROM conversation_shares WHERE share_code = $1",
        share_code,
    )
    if not row or not row["is_active"]:
        raise HTTPException(404, "Share not found")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(410, "Share has expired")

    snapshot = row["snapshot"] if isinstance(row["snapshot"], dict) else json.loads(row["snapshot"])
    artifact_ids = [a["artifact_id"] for a in snapshot.get("artifacts", [])]
    if artifact_id not in artifact_ids:
        raise HTTPException(404, "Artifact not in this share")

    artifact_storage = _get_artifact_storage(request)
    if not artifact_storage:
        raise HTTPException(503, "Artifact storage not available")

    try:
        artifact = await artifact_storage.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(404, "Artifact not found in storage")
        url = await artifact_storage.get_presigned_download_url(artifact)
        if url and urlsplit(url).scheme.lower() in {"http", "https"}:
            return RedirectResponse(url=url, status_code=302)

        content = await artifact_storage.download_artifact(artifact_id)
        if content is None:
            raise HTTPException(404, "Artifact content not found")
        return StreamingResponse(
            iter([content]),
            media_type=artifact.mime_type or "application/octet-stream",
            headers={
                "Content-Disposition": attachment_content_disposition(artifact.filename),
                "Content-Length": str(len(content)),
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get artifact URL: {e}")
        raise HTTPException(404, "Artifact not found")


# ── Authenticated: List User's Shares ────────────────────────────────


@router.get("/shares")
async def list_shares(
    request: Request,
    user: UserContext = Depends(get_user_context),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List shares created by the current user."""
    db = _get_db(request)
    rows = await db.fetch(
        """SELECT share_code, title, message_count, artifact_count, view_count,
                  is_active, created_at, expires_at
           FROM conversation_shares
           WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2""",
        user.user_id,
        limit,
    )
    return {"shares": [dict(r) for r in rows]}


# ── Authenticated: Revoke Share ──────────────────────────────────────


@router.delete("/shares/{share_code}")
async def revoke_share(
    share_code: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Revoke (deactivate) a share link."""
    db = _get_db(request)
    await db.execute(
        "UPDATE conversation_shares SET is_active = FALSE WHERE share_code = $1 AND user_id = $2",
        share_code,
        user.user_id,
    )
    return {"status": "revoked", "share_code": share_code}
