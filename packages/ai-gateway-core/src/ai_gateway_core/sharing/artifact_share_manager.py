"""
Artifact Share Manager — kind-generic public sharing of agent artifacts.

A share is a frozen snapshot: `payload` (public content) + `answer_keys`
(grading data, never served). kind='quiz' shares grade via QuizGrader and
persist attempts into quiz_attempts for continuity with the legacy share
flow; other kinds only bump attempt_count until a per-kind attempt store
is designed.
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ai_gateway_core.persistence import DatabaseStorageLike

logger = logging.getLogger(__name__)

SHARE_CODE_LENGTH = 12
MAX_DISPLAY_NAME_LEN = 100
DISPLAY_NAME_RE = re.compile(r"^[\w\s一-鿿؀-ۿ.\-@]+$", re.UNICODE)


def _sanitize_display_name(name: str | None) -> str | None:
    """Sanitize display_name: strip, length-limit, reject suspicious chars."""
    if not name:
        return None
    name = html.escape(name.strip())[:MAX_DISPLAY_NAME_LEN]
    if not name or not DISPLAY_NAME_RE.match(html.unescape(name)):
        return name  # keep escaped version even if regex fails
    return name


class ArtifactShareManager:
    """Manages kind-generic public artifact shares."""

    def __init__(self, db: DatabaseStorageLike) -> None:
        self.db = db

    async def create_share(
        self,
        *,
        kind: str,
        title: str,
        payload: dict[str, Any],
        answer_keys: dict[str, Any] | list[Any] | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        expires_hours: int | None = None,
        max_attempts: int | None = None,
        require_name: bool = True,
        time_limit_minutes: int | None = None,
    ) -> dict:
        """Create a shareable link with a frozen payload snapshot."""
        share_code = secrets.token_urlsafe(SHARE_CODE_LENGTH)[:SHARE_CODE_LENGTH]
        share_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=expires_hours) if expires_hours else None

        await self.db.execute(
            """
            INSERT INTO artifact_shares (id, share_code, kind, title, payload,
                                         answer_keys, tenant_id, created_by,
                                         is_active, max_attempts, expires_at,
                                         require_name, time_limit_minutes,
                                         created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9, $10, $11, $12, $13)
            """,
            uuid.UUID(share_id),
            share_code,
            kind,
            title,
            json.dumps(payload),
            json.dumps(answer_keys) if answer_keys is not None else None,
            tenant_id,
            user_id,
            max_attempts,
            expires_at,
            require_name,
            time_limit_minutes,
            now,
        )

        logger.info("Created %s share %s by %s", kind, share_code, user_id or "anonymous")
        return {
            "share_id": share_id,
            "share_code": share_code,
            "kind": kind,
            "title": title,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "require_name": require_name,
            "max_attempts": max_attempts,
            "time_limit_minutes": time_limit_minutes,
        }

    async def get_share_by_code(self, share_code: str) -> dict | None:
        """Look up an active share by its public code (expiry/max-attempts enforced)."""
        row = await self.db.fetchrow(
            """
            SELECT id, share_code, kind, title, payload, answer_keys,
                   tenant_id, created_by, is_active, max_attempts, expires_at,
                   require_name, time_limit_minutes, attempt_count, created_at
            FROM artifact_shares
            WHERE share_code = $1 AND is_active = TRUE
            """,
            share_code,
        )
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
            return None
        if row["max_attempts"] is not None and row["attempt_count"] >= row["max_attempts"]:
            return None

        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "share_id": str(row["id"]),
            "share_code": row["share_code"],
            "kind": row["kind"],
            "title": row["title"],
            "payload": payload,
            "answer_keys": row["answer_keys"],
            "tenant_id": row["tenant_id"],
            "created_by": row["created_by"],
            "require_name": row["require_name"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "time_limit_minutes": row["time_limit_minutes"],
        }

    async def get_public_artifact(self, share_code: str) -> dict | None:
        """Public snapshot: payload only, never answer keys."""
        share = await self.get_share_by_code(share_code)
        if not share:
            return None
        return {
            "share_code": share["share_code"],
            "kind": share["kind"],
            "title": share["title"],
            "require_name": share["require_name"],
            "time_limit_minutes": share["time_limit_minutes"],
            **share["payload"],
        }

    async def submit_attempt(
        self,
        share_code: str,
        answers: dict[str, str],
        display_name: str | None = None,
        client_ip: str | None = None,
    ) -> dict:
        """Submit an anonymous attempt against a shared artifact."""
        display_name = _sanitize_display_name(display_name)
        share = await self.get_share_by_code(share_code)
        if not share:
            raise ValueError("Share not found, expired, or max attempts reached")
        if share["require_name"] and not display_name:
            raise ValueError("This share requires a name before submitting.")

        # Dedup by display_name for named attempts. No IP dedup — shared
        # networks route many users through one IP; max_attempts bounds spam.
        share_uuid = uuid.UUID(share["share_id"])
        if display_name:
            dup = await self.db.fetchrow(
                "SELECT id FROM quiz_attempts WHERE share_id = $1 AND display_name = $2 LIMIT 1",
                share_uuid,
                display_name,
            )
            if dup:
                raise ValueError(
                    f"You have already submitted as '{html.unescape(display_name)}'."
                )

        result: dict[str, Any] = {}
        if share["kind"] == "quiz":
            result = await self._grade_quiz(share, answers, display_name, client_ip)
        else:
            # Generic kinds: record the attempt without grading.
            await self._reserve_attempt_slot(share)

        logger.info(
            "Public attempt on %s share %s: %s",
            share["kind"],
            share_code,
            result.get("correct_count", "recorded"),
        )
        return result

    async def _reserve_attempt_slot(self, share: dict) -> None:
        """Atomically consume one attempt slot, enforcing the cap under concurrency.

        The read-side check in ``get_share_by_code`` alone is a TOCTOU window:
        two concurrent submissions could both pass ``attempt_count < max_attempts``
        and both land. The conditional UPDATE closes it — Postgres applies the
        WHERE against the row version at statement time, so exactly one of the
        two succeeds once the cap is one slot away.
        """
        reserved = await self.db.execute(
            """
            UPDATE artifact_shares
            SET attempt_count = attempt_count + 1
            WHERE id = $1
              AND is_active = TRUE
              AND (expires_at IS NULL OR expires_at > NOW())
              AND (max_attempts IS NULL OR attempt_count < max_attempts)
            """,
            uuid.UUID(share["share_id"]),
        )
        if "UPDATE 1" not in reserved:
            raise ValueError("Share not found, expired, or max attempts reached")

    async def _grade_quiz(
        self,
        share: dict,
        answers: dict[str, str],
        display_name: str | None,
        client_ip: str | None,
    ) -> dict:
        from ai_gateway_core.quiz.quiz_grader import QuizGrader

        answer_keys = share["answer_keys"]
        if isinstance(answer_keys, str):
            answer_keys = json.loads(answer_keys)

        payload = share["payload"]
        quiz_id = payload.get("quiz_id") if isinstance(payload, dict) else None

        grader = QuizGrader()
        graded = grader.grade(answer_keys or [], answers)

        attempt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        await self.db.execute(
            """
            INSERT INTO quiz_attempts (id, quiz_id, user_id, share_id, display_name,
                                       answers, total_score, correct_count, total_count,
                                       started_at, completed_at, status, client_ip, exam_id)
            VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9, $9, 'completed', $10, NULL)
            """,
            uuid.UUID(attempt_id),
            uuid.UUID(quiz_id) if quiz_id else None,
            uuid.UUID(share["share_id"]),
            display_name,
            json.dumps(answers),
            graded["total_score"],
            graded["correct_count"],
            graded["total_count"],
            now,
            client_ip,
        )
        try:
            await self._reserve_attempt_slot(share)
        except ValueError:
            # Lost a concurrent race for the last slot: keep the attempt row
            # for history but mark it rejected so creators don't count it.
            await self.db.execute(
                "UPDATE quiz_attempts SET status = 'rejected' WHERE id = $1",
                uuid.UUID(attempt_id),
            )
            raise
        return {"attempt_id": attempt_id, **graded}

    async def revoke_share(self, share_id: str, user_id: str | None = None) -> bool:
        """Deactivate a share link."""
        if user_id is None:
            result = await self.db.execute(
                "UPDATE artifact_shares SET is_active = FALSE, revoked_at = NOW() WHERE id = $1",
                uuid.UUID(share_id),
            )
        else:
            result = await self.db.execute(
                "UPDATE artifact_shares SET is_active = FALSE, revoked_at = NOW() "
                "WHERE id = $1 AND created_by = $2",
                uuid.UUID(share_id),
                user_id,
            )
        return "UPDATE 1" in result
