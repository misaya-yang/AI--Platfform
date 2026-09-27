"""Current knowledge rights for a conversation's inherited context.

Core resumes the whole thread. Turning knowledge off for the next request does
not remove earlier source material. Use the existing admitted snapshots and
the knowledge service's current visible catalog; never create another ACL.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request

from ...core.auth.user_resolver import UserContext
from ..agent_runtime.thread_store import _safe_history_runtime_event


@dataclass(frozen=True)
class ConversationSources:
    dataset_ids: frozenset[str]
    inherited_by_run: dict[str, frozenset[str]]
    legacy_dataset_ids: frozenset[str]
    dated_runs: tuple[tuple[datetime, frozenset[str]], ...] = ()
    dates_complete: bool = False


def _object(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def conversation_sources(
    request: Request, user: UserContext, session_id: str,
) -> ConversationSources:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(503, detail={"code": "ASSISTANT_SOURCE_CHECK_UNAVAILABLE"})
    rows = await database.fetch(
        """SELECT run_id::text AS run_id, snapshot, created_at
             FROM assistant_runtime_snapshots
            WHERE session_id = $1 AND tenant_id = $2 AND user_id = $3
            ORDER BY created_at, snapshot_id""",
        session_id, user.tenant_id, user.user_id,
    )
    legacy_row = await database.fetchrow(
        "SELECT history FROM assistant.sessions "
        "WHERE session_id = $1 AND tenant_id = $2 AND user_id = $3",
        session_id, user.tenant_id, user.user_id,
    )
    history = _object(legacy_row["history"]) if legacy_row else []
    if isinstance(history, dict):
        history = history.get("messages", [])
    legacy_ids: set[str] = set()
    for message in history or []:
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") or {}
        for context in metadata.get("contexts", []) if isinstance(metadata, dict) else []:
            if isinstance(context, dict) and context.get("dataset_id"):
                legacy_ids.add(str(context["dataset_id"]))
    inherited = set(legacy_ids)
    by_run: dict[str, frozenset[str]] = {}
    dated_runs: list[tuple[datetime, frozenset[str]]] = []
    for row in rows:
        snapshot = _object(row["snapshot"])
        readonly = snapshot.get("readonly_capabilities") if isinstance(snapshot, dict) else None
        items = readonly.get("items") if isinstance(readonly, dict) else None
        if not isinstance(items, list):
            raise HTTPException(503, detail={"code": "ASSISTANT_SOURCE_CHECK_UNAVAILABLE"})
        for item in items:
            if isinstance(item, dict) and item.get("kind") == "knowledge":
                payload = item.get("payload") or {}
                dataset_id = payload.get("dataset_id") if isinstance(payload, dict) else None
                if not dataset_id:
                    raise HTTPException(503, detail={"code": "ASSISTANT_SOURCE_CHECK_UNAVAILABLE"})
                inherited.add(str(dataset_id))
        if row.get("run_id"):
            by_run[str(row["run_id"])] = frozenset(inherited)
        if isinstance(row.get("created_at"), datetime):
            dated_runs.append((row["created_at"], frozenset(inherited)))
    return ConversationSources(frozenset(inherited), by_run, frozenset(legacy_ids), tuple(dated_runs), len(dated_runs) == len(rows))


async def visible_dataset_names(request: Request, user: UserContext) -> dict[str, str]:
    proxy = getattr(request.app.state, "kb_proxy", None)
    if not callable(getattr(proxy, "list_datasets", None)):
        return {}
    try:
        return {
            str(dataset["dataset_id"]): str(dataset.get("name") or dataset["dataset_id"])
            for dataset in await proxy.list_datasets(user)
            if isinstance(dataset, dict) and dataset.get("dataset_id")
        }
    except Exception:
        return {}


async def quiz_source_ids(
    request: Request, quiz_id: UUID, tenant_id: str, *, require_origin: bool = False,
) -> frozenset[str]:
    """Resolve the creating run, including sources from earlier turns."""
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    row = await db.fetchrow(
        """SELECT q.dataset_ids, q.created_by,
                  e.session_id, e.run_id::text AS run_id
             FROM assistant.quizzes q
             LEFT JOIN assistant_capability_executions e
               ON e.tenant_id = q.tenant_id AND e.user_id = q.created_by
              AND e.capability_id = 'generate_quiz'
              AND COALESCE(e.result_summary ->> 'quiz_id',
                           e.result_summary #>> '{result,quiz_id}') = q.id::text
            WHERE q.id = $1 AND q.tenant_id = $2
            ORDER BY e.created_at LIMIT 1""",
        quiz_id, tenant_id,
    )
    if row is None:
        raise HTTPException(404, "Quiz not found")
    ids = _object(row["dataset_ids"] or [])
    if not isinstance(ids, list):
        raise HTTPException(503, detail={"code": "ASSISTANT_SOURCE_CHECK_UNAVAILABLE"})
    sources = set(ids)
    verified_origin = False
    if row.get("session_id"):
        owner = UserContext(user_id=str(row["created_by"]), tenant_id=tenant_id, is_authenticated=True)
        inherited = await conversation_sources(request, owner, row["session_id"])
        run_id = str(row["run_id"])
        verified_origin = run_id in inherited.inherited_by_run
        sources.update(inherited.inherited_by_run.get(run_id, inherited.dataset_ids))
    if sources:
        return frozenset(sources)
    if require_origin and not verified_origin:
        raise HTTPException(409, detail={"code": "ASSISTANT_SOURCE_ORIGIN_UNVERIFIED"})
    return frozenset()


async def require_public_quiz_source_access(request: Request, share_code: str) -> None:
    """Old frozen links also need verifiable, non-private origins."""
    db = getattr(request.app.state, "database", None)
    if db is None:
        raise HTTPException(503, "Database not available")
    row = await db.fetchrow(
        "SELECT payload, tenant_id FROM assistant.artifact_shares "
        "WHERE share_code = $1 AND kind = 'quiz'", share_code,
    )
    unavailable = HTTPException(404, detail={"code": "share_unavailable", "message": "Quiz not found or expired"})
    if row is None:
        raise unavailable
    payload = _object(row["payload"])
    try:
        quiz_id = UUID(str(payload.get("quiz_id")))
    except (ValueError, AttributeError):
        raise unavailable from None
    try:
        sources = await quiz_source_ids(request, quiz_id, row["tenant_id"], require_origin=True)
    except HTTPException as exc:
        if exc.status_code in {404, 409}:
            raise unavailable from None
        raise
    if sources:
        raise unavailable


async def require_conversation_source_access(
    request: Request, user: UserContext, session_id: str, *, for_execution: bool = False,
) -> None:
    sources = await conversation_sources(request, user, session_id)
    if not sources.dataset_ids:
        return
    visible = await visible_dataset_names(request, user)
    if not sources.dataset_ids <= visible.keys():
        raise HTTPException(
            409 if for_execution else 403,
            detail={
                "code": "ASSISTANT_SOURCE_ACCESS_REVOKED",
                "message": "Earlier knowledge sources are unavailable. Start a new conversation to continue.",
            },
        )


def source_ids_at_creation(sources: ConversationSources, created_at: Any) -> frozenset[str]:
    ids = sources.dataset_ids
    if isinstance(created_at, datetime) and sources.dates_complete:
        # Server-owned creation times preserve an earlier ordinary artifact
        # when only a later turn selected the now-unavailable knowledge.
        ids = sources.legacy_dataset_ids
        for admitted_at, inherited in sources.dated_runs:
            admitted_utc = admitted_at if admitted_at.tzinfo else admitted_at.replace(tzinfo=timezone.utc)
            created_utc = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            if admitted_utc <= created_utc:
                ids = inherited
    return ids


async def require_artifact_source_access(request: Request, user: UserContext, artifact: Any) -> None:
    if getattr(artifact, "source", None) == "user":
        return  # Uploaded originals do not inherit generated-answer provenance.
    sources = await conversation_sources(request, user, artifact.session_id)
    ids = source_ids_at_creation(sources, getattr(artifact, "created_at", None))
    if ids and not ids <= (await visible_dataset_names(request, user)).keys():
        raise HTTPException(403, detail={"code": "ASSISTANT_SOURCE_ACCESS_REVOKED"})


class ConversationEventGuard:
    """Recheck long-lived reads and expose only public task facts on denial."""

    def __init__(self, request: Request, user: UserContext, sources: ConversationSources, *, session_id: str):
        self.request = request
        self.user = user
        self.sources = sources
        self.seen_run_ids = set(sources.inherited_by_run)
        self.sources_session_id = session_id
        self.dataset_ids = sources.dataset_ids
        self.next_check = 0.0
        self.revoked = False

    async def project(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        run_id = str(data.get("run_id") or "")
        if run_id and run_id not in self.seen_run_ids:
            self.sources = await conversation_sources(self.request, self.user, self.sources_session_id)
            self.seen_run_ids.add(run_id)
        ids = self.sources.inherited_by_run.get(run_id, self.sources.dataset_ids)
        if ids != self.dataset_ids:
            self.dataset_ids = ids
            self.next_check = 0.0
        if not self.dataset_ids:
            return raw
        now = monotonic()
        if now >= self.next_check:
            visible = await visible_dataset_names(self.request, self.user)
            self.revoked = not self.dataset_ids <= visible.keys()
            self.next_check = now + 1.0
        if not self.revoked:
            return raw
        event_type = raw.get("event_type")
        safe = _safe_history_runtime_event(raw)
        public = dict(safe.get("data") or {}) if event_type in {
            "run_finished", "run_error", "cancelled", "side_effect_unknown",
        } else {}
        for key in ("run_id", "tool_call_id", "approval_id"):
            if isinstance(data.get(key), str):
                public[key] = data[key]
        if data.get("status") in {"pending", "approved", "rejected", "expired", "cancelled"}:
            public["status"] = data["status"]
        if isinstance(data.get("approved"), bool):
            public["approved"] = data["approved"]
        public["source_access_revoked"] = True
        return {
            "sequence": raw.get("sequence"), "timestamp": raw.get("timestamp"),
            "event_id": raw.get("event_id"), "event_key": raw.get("event_key"),
            "event_type": event_type if event_type in {
                "run_started", "approval_required", "approval_result",
                "run_finished", "run_error", "cancelled", "side_effect_unknown",
            } else "source_access_revoked",
            "data": public,
        }


async def guarded_conversation_frames(
    request: Request, user: UserContext, session_id: str, frames: AsyncIterator[bytes],
) -> AsyncIterator[bytes]:
    guard = ConversationEventGuard(request, user, await conversation_sources(request, user, session_id), session_id=session_id)
    async for frame in frames:
        lines = frame.decode("utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.startswith("data:"):
                continue
            raw = json.loads(line[5:].strip())
            projected = await guard.project(raw)
            lines[index] = "data: " + json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
            if projected.get("event_type") != raw.get("event_type"):
                lines = [f"event: {projected['event_type']}" if item.startswith("event:") else item for item in lines]
        yield ("\n".join(lines) + "\n\n").encode()


def runtime_source_access_checker(app: Any):
    """Recheck original source rights before any resumed model request."""
    async def check(*, tenant_id: str, user_id: str, session_id: str, run_id: str) -> bool:
        request = Request({"type": "http", "headers": [], "app": app})
        owner = UserContext(user_id=user_id, tenant_id=tenant_id, is_authenticated=True)
        sources = await conversation_sources(request, owner, session_id)
        ids = sources.inherited_by_run.get(run_id, sources.dataset_ids)
        if not ids:
            return True
        # Current database identity supplies roles; recovered metadata cannot
        # resurrect an earlier administrator role or a disabled account.
        profile = await app.state.database.get_user(user_id)
        if not profile or profile.get("status") != "active" or profile.get("tenant_id") != tenant_id:
            return False
        roles = profile.get("roles") or []
        if isinstance(roles, str):
            roles = json.loads(roles)
        actor = UserContext(
            user_id=user_id, tenant_id=tenant_id, is_authenticated=True,
            roles=roles, tier=str(profile.get("tier") or "normal"),
        )
        return ids <= (await visible_dataset_names(request, actor)).keys()
    return check
