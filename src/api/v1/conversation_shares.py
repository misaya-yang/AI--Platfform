"""Conversation Share API — share assistant conversations with artifacts as public snapshots."""

from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ...core.observability.logging import get_logger
from ..deps import get_user_context

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

    # Load session
    session = await db.fetchrow(
        "SELECT session_id, history, metadata, user_id, tenant_id FROM sessions WHERE session_id = $1",
        session_id,
    )
    if not session:
        raise HTTPException(404, "Session not found")

    # Verify ownership
    if session["user_id"] and session["user_id"] != user.user_id:
        raise HTTPException(403, "Not your session")

    history = json.loads(session["history"]) if isinstance(session["history"], str) else session["history"]
    if not history:
        raise HTTPException(400, "Session has no messages")

    # Load artifacts for this session
    artifacts_data = []
    if body.include_artifacts:
        rows = await db.fetch(
            "SELECT artifact_id, type, format, title, filename, size_bytes, mime_type, source FROM artifacts WHERE session_id = $1",
            session_id,
        )
        artifacts_data = [dict(r) for r in rows]

    # Build snapshot
    meta = json.loads(session["metadata"]) if isinstance(session["metadata"], str) else (session["metadata"] or {})
    title = meta.get("title", "") if isinstance(meta, dict) else ""
    model_id = None
    for msg in reversed(history):
        if isinstance(msg, dict) and msg.get("metadata", {}).get("model_id"):
            model_id = msg["metadata"]["model_id"]
            break

    snapshot = {
        "messages": history,
        "artifacts": artifacts_data,
        "model_id": model_id,
        "shared_at": datetime.now(timezone.utc).isoformat(),
    }

    # Generate unique share code
    share_code = _generate_share_code()
    for _ in range(5):
        existing = await db.fetchrow(
            "SELECT id FROM conversation_shares WHERE share_code = $1", share_code
        )
        if not existing:
            break
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
    logger.info(f"Created share {share_code} for session {session_id} ({len(history)} msgs, {len(artifacts_data)} artifacts)")

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

    # Check expiry
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(410, "Share has expired")

    # Increment view count (fire-and-forget)
    try:
        await db.execute(
            "UPDATE conversation_shares SET view_count = view_count + 1 WHERE share_code = $1",
            share_code,
        )
    except Exception:
        pass

    snapshot = json.loads(row["snapshot"]) if isinstance(row["snapshot"], str) else row["snapshot"]

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

    # Verify artifact is in the share snapshot
    snapshot = json.loads(row["snapshot"]) if isinstance(row["snapshot"], str) else row["snapshot"]
    artifact_ids = [a["artifact_id"] for a in snapshot.get("artifacts", [])]
    if artifact_id not in artifact_ids:
        raise HTTPException(404, "Artifact not in this share")

    # Proxy to artifact storage
    artifact_storage = _get_artifact_storage(request)
    if not artifact_storage:
        raise HTTPException(503, "Artifact storage not available")

    try:
        url = await artifact_storage.get_download_url(artifact_id)
        return RedirectResponse(url=url, status_code=302)
    except Exception as e:
        logger.error(f"Failed to get artifact URL: {e}")
        raise HTTPException(404, "Artifact not found")


# ── Authenticated: List User's Shares ────────────────────────────────


@router.get("/shares")
async def list_shares(
    request: Request,
    user: UserContext = Depends(get_user_context),
    limit: int = 50,
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
    result = await db.execute(
        "UPDATE conversation_shares SET is_active = FALSE WHERE share_code = $1 AND user_id = $2",
        share_code,
        user.user_id,
    )
    return {"status": "revoked", "share_code": share_code}
