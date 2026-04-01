"""
Exam Management API — Admin-only CRUD, stats, and AI analysis for exams.

Endpoints:
- POST   /exams                         — Create exam from quiz
- GET    /exams                         — List exams (paginated, filterable)
- GET    /exams/{id}                    — Get exam detail
- PATCH  /exams/{id}                    — Update exam settings
- POST   /exams/{id}/publish            — Publish exam (activates share link)
- POST   /exams/{id}/close              — Close exam (stops submissions)
- GET    /exams/{id}/attempts           — List attempts (paginated)
- GET    /exams/{id}/attempts/export    — CSV export
- GET    /exams/{id}/stats              — Aggregate statistics
- POST   /exams/{id}/analyze            — Generate AI analysis report
- GET    /exams/{id}/reports            — List analysis reports
- GET    /exams/{id}/reports/{rid}      — Get specific report
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ..deps import get_user_context
from ...services.assistant.exam_service import ExamService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exams", tags=["exams"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExamCreateRequest(BaseModel):
    quiz_id: str
    title: str
    description: str | None = None
    assigned_groups: list[str] = Field(default_factory=list)
    deadline: datetime | None = None
    max_retakes: int = 1
    time_limit_minutes: int | None = None
    passing_score: float = 0.6


class ExamUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    assigned_groups: list[str] | None = None
    deadline: datetime | None = None
    max_retakes: int | None = None
    time_limit_minutes: int | None = None
    passing_score: float | None = None
    settings: dict | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_exam_service(request: Request) -> ExamService:
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    return ExamService(db=db)


def _require_exam_permission(user: UserContext, permission: str = "exam:manage"):
    """Check that user has exam management permission (admin)."""
    # admin:* wildcard matches everything
    user_perms = set(user.permissions or [])
    for role in (user.roles or []):
        if role == "admin":
            return  # admin role has admin:* which matches all
    # Direct permission check
    if permission in user_perms:
        return
    # Wildcard check
    parts = permission.split(":")
    for i in range(len(parts) - 1, 0, -1):
        if ":".join(parts[:i]) + ":*" in user_perms:
            return
    raise HTTPException(403, f"Permission denied: {permission} required")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("")
async def create_exam(
    body: ExamCreateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:manage")
    svc = _get_exam_service(request)
    try:
        return await svc.create_exam(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            quiz_id=body.quiz_id,
            title=body.title,
            description=body.description,
            assigned_groups=body.assigned_groups,
            deadline=body.deadline,
            max_retakes=body.max_retakes,
            time_limit_minutes=body.time_limit_minutes,
            passing_score=body.passing_score,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("")
async def list_exams(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    return await svc.list_exams(user.tenant_id, status=status, limit=limit, offset=offset)


@router.get("/{exam_id}")
async def get_exam(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    exam = await svc.get_exam(exam_id, user.tenant_id)
    if not exam:
        raise HTTPException(404, "Exam not found")
    return exam


@router.patch("/{exam_id}")
async def update_exam(
    exam_id: str,
    body: ExamUpdateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:manage")
    svc = _get_exam_service(request)
    updated = await svc.update_exam(
        exam_id, user.tenant_id,
        **body.model_dump(exclude_none=True),
    )
    if not updated:
        raise HTTPException(404, "Exam not found or no changes")
    return {"updated": True}


@router.post("/{exam_id}/publish")
async def publish_exam(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:manage")
    svc = _get_exam_service(request)
    try:
        return await svc.publish_exam(exam_id, user.tenant_id, user.user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{exam_id}/close")
async def close_exam(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:manage")
    svc = _get_exam_service(request)
    try:
        await svc.close_exam(exam_id, user.tenant_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "closed"}


# ---------------------------------------------------------------------------
# Attempts & Stats
# ---------------------------------------------------------------------------

@router.get("/{exam_id}/attempts")
async def list_attempts(
    exam_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    return await svc.list_attempts(exam_id, user.tenant_id, limit=limit, offset=offset)


@router.get("/{exam_id}/attempts/export")
async def export_attempts_csv(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    data = await svc.list_attempts(exam_id, user.tenant_id, limit=10000)
    attempts = data.get("attempts", [])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Score", "Correct", "Total", "Percentage", "Completed At", "IP", "Status"])
    for a in attempts:
        pct = f"{a['total_score'] * 100:.0f}%" if a.get("total_score") is not None else ""
        writer.writerow([
            a.get("display_name") or a.get("user_id") or "Anonymous",
            a.get("total_score", ""),
            a.get("correct_count", ""),
            a.get("total_count", ""),
            pct,
            a.get("completed_at", ""),
            a.get("client_ip", ""),
            a.get("status", ""),
        ])

    exam = await svc.get_exam(exam_id, user.tenant_id)
    filename = f"exam-{(exam or {}).get('title', exam_id[:8])}-results.csv"

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{exam_id}/stats")
async def get_stats(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    try:
        return await svc.get_stats(exam_id, user.tenant_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# AI Analysis
# ---------------------------------------------------------------------------

@router.post("/{exam_id}/analyze")
async def analyze_exam(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:analyze")
    svc = _get_exam_service(request)
    registry = getattr(request.app.state, "model_registry", None)
    if not registry:
        raise HTTPException(503, "Model registry not available")
    try:
        return await svc.generate_analysis(exam_id, user.tenant_id, registry)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(f"Exam analysis failed: {e}", exc_info=True)
        raise HTTPException(500, "Analysis generation failed. Please try again.")


@router.get("/{exam_id}/reports")
async def list_reports(
    exam_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    return {"reports": await svc.list_reports(exam_id)}


@router.get("/{exam_id}/reports/{report_id}")
async def get_report(
    exam_id: str,
    report_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    _require_exam_permission(user, "exam:view")
    svc = _get_exam_service(request)
    report = await svc.get_report(report_id)
    if not report or report["exam_id"] != exam_id:
        raise HTTPException(404, "Report not found")
    return report
