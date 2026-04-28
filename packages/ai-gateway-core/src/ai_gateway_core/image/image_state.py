"""Image multi-turn state persistence — backed by Postgres.

Tables (see ``database/migrations/per_service/assistant/001_image_session_artifacts.sql``):

* ``assistant.image_sessions`` — focused (owner, session) state mirror with
  ``latest_artifact_id`` pointer + ``locked_style``.
* ``assistant.image_turns``    — per-turn audit trail (prompt, parent, output,
  status, error).
* ``assistant.image_idempotency`` — dedup of ``client_request_id`` within an
  ``owner_scope``.

This module is intentionally a thin layer of explicit asyncpg calls — no
ORM. Callers pass an ``asyncpg``-style pool (``database._pool``) and get
back plain dicts. Concurrency control: ``advance_latest_artifact_cas`` is
the ``image_sessions`` compare-and-swap used for the
``expected_parent_artifact_id`` race-detection contract.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _db_safe(default):
    """Wrap async DB helpers so a non-asyncpg pool (test mocks, missing DB,
    transient outage) degrades to ``default`` instead of raising.

    ``default`` may be a callable factory for mutable defaults (lists/tuples).
    Real CAS conflicts inside the function still surface as ``False``; only
    accidental TypeError / AttributeError / asyncpg errors get swallowed."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s: DB unavailable (%s) — fallback", fn.__name__, exc)
                return default() if callable(default) else default
        return wrapper
    return deco


# ----- Owner scope ---------------------------------------------------------

_OWNER_SCOPE_SEP = "\x1f"  # ASCII unit separator — not a legal identifier char


def compute_owner_scope(
    user_id: str,
    *,
    app_tenant_id: str | None = None,
    app_user_id: str | None = None,
) -> str:
    """Compute the opaque owner-scope identity key.

    Format:
      * Both ``app_tenant_id`` AND ``app_user_id`` present:
          ``f"{user_id}\\x1f{app_tenant_id}\\x1f{app_user_id}"``
        — separator is ASCII unit separator (0x1F), which is not a legal
        character in tenant / user identifiers in any of our backends. Using
        a printable separator like ``|`` would let a caller forge collisions
        by embedding the separator in their identifiers (e.g. send
        ``app_user_id="alice|admin"`` to align with another caller's scope).
      * Else: fallback to ``user_id`` (legacy single-tenant flow).

    Treat the returned string as opaque — never parse, never display.
    """
    if app_tenant_id and app_user_id:
        if _OWNER_SCOPE_SEP in app_tenant_id or _OWNER_SCOPE_SEP in app_user_id:
            # Defense-in-depth: should never happen since our headers are
            # http-cleaned, but if a control char ever slips through reject
            # the request rather than poison the scope namespace.
            raise ValueError(
                "app_tenant_id / app_user_id must not contain control characters"
            )
        return f"{user_id}{_OWNER_SCOPE_SEP}{app_tenant_id}{_OWNER_SCOPE_SEP}{app_user_id}"
    return user_id


# ----- Request hashing -----------------------------------------------------

def compute_request_hash(payload: dict[str, Any]) -> str:
    """Sha256-hex of the canonical JSON encoding of a request payload.

    Used by the idempotency layer: same ``client_request_id`` + same
    ``request_hash`` → idempotent replay returns the original task_id.
    Same client_request_id + *different* hash → 409, the caller is
    re-using the id with new content.

    ``client_request_id`` itself is excluded from the hash so replay of
    the original request is detected.
    """
    cleaned = {k: v for k, v in payload.items() if k != "client_request_id"}
    canonical = json.dumps(cleaned, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_turn_id() -> str:
    return f"itn_{uuid.uuid4().hex[:16]}"


# ----- image_sessions ------------------------------------------------------

@_db_safe(None)
async def get_image_session(pool, session_id: str) -> dict | None:
    """Return the image_sessions row for ``session_id`` (or None)."""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM assistant.image_sessions WHERE session_id = $1",
            session_id,
        )
    return dict(row) if row else None


@_db_safe(None)
async def upsert_image_session(
    pool,
    *,
    session_id: str,
    owner_scope: str,
    app_user_id: str | None,
    app_tenant_id: str | None,
    locked_style: str | None = None,
) -> None:
    """Create-or-touch an image_sessions row.

    Does NOT change ``latest_artifact_id`` — that's owned by
    ``advance_latest_artifact_cas``. Style writes are explicit via
    ``set_locked_style``.
    """
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO assistant.image_sessions
                (session_id, owner_scope, app_user_id, app_tenant_id, locked_style)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (session_id) DO UPDATE SET
                updated_at = NOW(),
                locked_style = COALESCE(EXCLUDED.locked_style, assistant.image_sessions.locked_style)
            """,
            session_id,
            owner_scope,
            app_user_id,
            app_tenant_id,
            locked_style,
        )


@_db_safe(None)
async def set_locked_style(pool, session_id: str, style: str | None) -> None:
    """Overwrite ``locked_style``. ``None`` clears the lock."""
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE assistant.image_sessions SET locked_style = $1, updated_at = NOW() "
            "WHERE session_id = $2",
            style,
            session_id,
        )


@_db_safe(False)
async def advance_latest_artifact_cas(
    pool,
    *,
    session_id: str,
    expected_parent: str | None,
    new_artifact_id: str,
) -> bool:
    """Compare-and-swap update of ``latest_artifact_id``.

    Returns True iff the row's current ``latest_artifact_id`` equals
    ``expected_parent`` (NULL-safe) and the swap succeeded. The caller is
    expected to have *just* generated against the parent; if a competing
    request overwrote ``latest_artifact_id`` in the meantime we leave the
    pointer pointing at the racer's output and return False.

    NOTE: callers must have created the image_sessions row first via
    ``upsert_image_session``. We use SELECT FOR UPDATE inside a tx to
    serialize concurrent writers on the same session_id.
    """
    if pool is None:
        return False
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT latest_artifact_id FROM assistant.image_sessions "
                "WHERE session_id = $1 FOR UPDATE",
                session_id,
            )
            if row is None:
                # Row missing — caller forgot to upsert. Refuse to silently
                # create here; CAS semantics demand the row exist.
                return False
            current = row["latest_artifact_id"]
            if current != expected_parent:
                return False
            await conn.execute(
                "UPDATE assistant.image_sessions "
                "SET latest_artifact_id = $1, updated_at = NOW() "
                "WHERE session_id = $2",
                new_artifact_id,
                session_id,
            )
            return True


# ----- image_turns ---------------------------------------------------------

@_db_safe(None)
async def insert_turn(
    pool,
    *,
    turn_id: str,
    session_id: str,
    owner_scope: str,
    task_id: str | None,
    prompt: str | None,
    model_id: str | None,
    style: str | None,
    add_watermark: bool,
    parent_artifact_id: str | None,
    output_artifact_id: str | None,
    status: str,
    error: str | None,
    error_code: str | None,
    client_request_id: str | None,
    request_hash: str | None,
    completed_at: datetime | None = None,
) -> None:
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO assistant.image_turns (
                turn_id, session_id, owner_scope, task_id, prompt, model_id,
                style, add_watermark, parent_artifact_id, output_artifact_id,
                status, error, error_code, client_request_id, request_hash,
                completed_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
            )
            ON CONFLICT (turn_id) DO UPDATE SET
                status = EXCLUDED.status,
                error = EXCLUDED.error,
                error_code = EXCLUDED.error_code,
                output_artifact_id = COALESCE(EXCLUDED.output_artifact_id, assistant.image_turns.output_artifact_id),
                completed_at = COALESCE(EXCLUDED.completed_at, assistant.image_turns.completed_at)
            """,
            turn_id, session_id, owner_scope, task_id, prompt, model_id,
            style, add_watermark, parent_artifact_id, output_artifact_id,
            status, error, error_code, client_request_id, request_hash,
            completed_at,
        )


@_db_safe(None)
async def update_turn_status(
    pool,
    *,
    turn_id: str,
    status: str,
    output_artifact_id: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE assistant.image_turns SET
                status = $2,
                output_artifact_id = COALESCE($3, output_artifact_id),
                error = $4,
                error_code = $5,
                completed_at = CASE
                    WHEN $2 IN ('completed', 'failed') THEN NOW()
                    ELSE completed_at
                END
            WHERE turn_id = $1
            """,
            turn_id, status, output_artifact_id, error, error_code,
        )


@_db_safe(None)
async def get_turn(pool, turn_id: str) -> dict | None:
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM assistant.image_turns WHERE turn_id = $1",
            turn_id,
        )
    return dict(row) if row else None


@_db_safe(None)
async def get_turn_by_task(pool, task_id: str) -> dict | None:
    """Look up a turn by its async task_id (poll fallback)."""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM assistant.image_turns WHERE task_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            task_id,
        )
    return dict(row) if row else None


@_db_safe(lambda: ([], None))
async def list_turns(
    pool,
    *,
    session_id: str,
    owner_scope: str,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    """Return (turns, next_cursor). Cursor is the ``created_at`` ISO of the
    last row returned — pagination is "older than this timestamp".

    Tied with ``turn_id`` for stable ordering when timestamps collide.
    """
    if pool is None:
        return [], None

    args: list[Any] = [session_id, owner_scope]
    where_extra = ""
    if cursor:
        # cursor format "ISO|turn_id" — keysetted on (created_at DESC, turn_id DESC)
        try:
            parts = cursor.split("|", 1)
            cur_ts = datetime.fromisoformat(parts[0])
            cur_turn = parts[1] if len(parts) > 1 else ""
            args.extend([cur_ts, cur_turn])
            where_extra = "AND (created_at, turn_id) < ($3, $4)"
        except Exception:
            logger.warning("Invalid cursor %r — ignoring", cursor)

    sql = f"""
        SELECT * FROM assistant.image_turns
        WHERE session_id = $1 AND owner_scope = $2
        {where_extra}
        ORDER BY created_at DESC, turn_id DESC
        LIMIT {int(min(max(limit, 1), 200)) + 1}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    rows_d = [dict(r) for r in rows]
    next_cursor: str | None = None
    requested = int(min(max(limit, 1), 200))
    if len(rows_d) > requested:
        last = rows_d[requested - 1]
        next_cursor = f"{last['created_at'].isoformat()}|{last['turn_id']}"
        rows_d = rows_d[:requested]
    return rows_d, next_cursor


# ----- Idempotency --------------------------------------------------------

@_db_safe(None)
async def lookup_idempotent(
    pool,
    *,
    owner_scope: str,
    client_request_id: str,
) -> dict | None:
    """Return the recorded (request_hash, task_id) for this key, or None."""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT request_hash, task_id, created_at FROM assistant.image_idempotency "
            "WHERE owner_scope = $1 AND client_request_id = $2",
            owner_scope, client_request_id,
        )
    return dict(row) if row else None


@_db_safe(False)
async def record_idempotent(
    pool,
    *,
    owner_scope: str,
    client_request_id: str,
    request_hash: str,
    task_id: str,
) -> bool:
    """Record an idempotent claim. Returns True on insert, False on existing."""
    if pool is None:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO assistant.image_idempotency
                (owner_scope, client_request_id, request_hash, task_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (owner_scope, client_request_id) DO NOTHING
            """,
            owner_scope, client_request_id, request_hash, task_id,
        )
    # asyncpg "INSERT 0 1" / "INSERT 0 0"
    try:
        return result.endswith(" 1")
    except AttributeError:
        return True


__all__ = [
    "advance_latest_artifact_cas",
    "compute_owner_scope",
    "compute_request_hash",
    "get_image_session",
    "get_turn",
    "get_turn_by_task",
    "insert_turn",
    "list_turns",
    "lookup_idempotent",
    "new_turn_id",
    "record_idempotent",
    "set_locked_style",
    "update_turn_status",
    "upsert_image_session",
]
