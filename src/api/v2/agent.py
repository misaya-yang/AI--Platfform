"""Native V2 Thread/Turn/Item boundary.

V1 remains a compatibility projection.  V2 is intentionally thin: ownership,
assignment, legacy import, and durable cursor reads live in the Gateway while
the Agent Runtime remains the only Agent loop for every session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from ai_gateway_core.exceptions import SessionAlreadyExistsError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...core.auth.user_resolver import UserContext
from ...services.agent_runtime.control_plane import AgentRuntimeControlError
from ...services.agent_runtime.thread_store import (
    AgentThreadStore,
    RuntimeThread,
)
from ..deps import get_user_context

router = APIRouter(prefix="/agent", tags=["Agent Runtime V2"])
logger = logging.getLogger(__name__)


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)


class TurnCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=200_000)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    reasoning_option: str | None = Field(default=None, max_length=100)
    thinking_level: str | None = Field(default=None, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    execution_profile: str = Field(default="safe", max_length=32)
    memory_mode: str = Field(default="auto", max_length=32)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    os_agent_enabled: bool = False
    local_node_device_id: str | None = Field(default=None, max_length=128)
    local_node_grant_ids: list[str] = Field(default_factory=list, max_length=100)
    resume_run_id: str | None = Field(default=None, max_length=255)
    resume_approval_id: str | None = Field(default=None, max_length=255)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    kb_dataset_ids: list[str] = Field(default_factory=list, max_length=100)
    kb_mode: str = Field(default="off", max_length=20)
    kb_top_k: int = Field(default=5, ge=1, le=20)
    kb_score_threshold: float = Field(default=0.4, ge=0, le=1)
    web_search_enabled: bool = False
    web_search_max_results: int = Field(default=5, ge=1, le=20)
    file_paths: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def reject_ambiguous_reasoning(self) -> TurnCreateRequest:
        if self.reasoning_option and self.thinking_level:
            raise ValueError("reasoning_option and thinking_level are mutually exclusive")
        if self.kb_mode not in {"auto", "tool", "off"}:
            raise ValueError("unsupported knowledge mode")
        return self


class InterruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="client_interrupt", min_length=1, max_length=100)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str | None = Field(default=None, max_length=500)


def _store(request: Request) -> AgentThreadStore:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_STORAGE_UNAVAILABLE"})
    value = getattr(request.app.state, "agent_thread_store", None)
    if value is None:
        value = AgentThreadStore(database)
        request.app.state.agent_thread_store = value
    return value


def _require_actor(user: UserContext) -> None:
    if not user.is_authenticated or not user.user_id:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED"})
    if not user.tenant_id or user.tenant_id == "public":
        raise HTTPException(status_code=403, detail={"code": "TENANT_REQUIRED"})


def _reject_unmigrated_turn_capabilities(body: TurnCreateRequest) -> None:
    """Keep V2 fail-closed until these controls have a runtime contract."""

    unsupported = (
        body.execution_profile != "safe"
        or body.memory_mode not in {"auto", "strict", "off"}
        or body.system_prompt is not None
        or body.os_agent_enabled
        or body.local_node_device_id is not None
        or bool(body.local_node_grant_ids)
        or body.resume_run_id is not None
        or body.resume_approval_id is not None
    )
    if unsupported:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENT_RUNTIME_CAPABILITY_NOT_MIGRATED",
                "message": "This capability is not available on the Agent Runtime yet",
            },
        )


async def _assignment(request: Request, user: UserContext, session_id: str) -> Any:
    assignments = getattr(request.app.state, "assistant_runtime_assignments", None)
    if assignments is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_ASSIGNMENT_UNAVAILABLE"})
    assignment = await assignments.resolve(
        tenant_id=user.tenant_id, user_id=user.user_id, session_id=session_id
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail={"code": "AGENT_RUNTIME_ASSIGNMENT_NOT_FOUND"})
    if assignment.runtime_owner != "agent_runtime":
        raise HTTPException(
            status_code=409,
            detail={"code": "AGENT_RUNTIME_NOT_ASSIGNED", "runtime_owner": assignment.runtime_owner},
        )
    return assignment


async def _bind_new_assignment(request: Request, user: UserContext, session_id: str) -> Any:
    assignments = getattr(request.app.state, "assistant_runtime_assignments", None)
    if assignments is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_ASSIGNMENT_UNAVAILABLE"})
    policy = getattr(request.app.state, "assistant_runtime_assignment_policy", None)
    if policy is not None and hasattr(assignments, "bind_new_session"):
        return await assignments.bind_new_session(
            tenant_id=user.tenant_id, user_id=user.user_id,
            session_id=session_id, policy=policy,
        )
    return await assignments.bind(
        tenant_id=user.tenant_id, user_id=user.user_id, session_id=session_id,
        runtime_owner="agent_runtime",
        kernel_revision=getattr(request.app.state, "assistant_runtime_kernel_revision", None),
        assignment_reason="v2_thread_create",
    )


def _thread_payload(thread: RuntimeThread) -> dict[str, Any]:
    return {
        "schema_version": "agent-thread/v2",
        "id": thread.runtime_thread_id,
        "thread_id": thread.runtime_thread_id,
        "session_id": thread.session_id,
        "runtime": {"owner": thread.kernel_owner, "source": thread.source_kind},
        "import_status": thread.import_status,
        "last_sequence": thread.last_sequence,
    }


@router.post("/threads", status_code=201)
async def create_thread(
    body: ThreadCreateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    _require_actor(user)
    session_manager = getattr(request.app.state, "session_manager", None)
    if session_manager is None:
        raise HTTPException(status_code=503, detail={"code": "SESSION_STORAGE_UNAVAILABLE"})

    session_id = body.session_id
    created_here = False
    if session_id:
        session = await session_manager.get(session_id)
        if session and (session.user_id != user.user_id or session.tenant_id != user.tenant_id):
            raise HTTPException(status_code=404, detail={"code": "SESSION_NOT_FOUND"})
        if session is None:
            # The Web client mints the session id before opening the stream.
            # Persist that id atomically; an existing owner is never adopted.
            try:
                session = await session_manager.create(
                    user_id=user.user_id,
                    tenant_id=user.tenant_id,
                    service_id="__builtin_assistant__",
                    session_id=session_id,
                    fail_if_exists=True,
                )
                created_here = True
            except SessionAlreadyExistsError:
                session = await session_manager.get(session_id)
                if not session or session.user_id != user.user_id or session.tenant_id != user.tenant_id:
                    raise HTTPException(status_code=404, detail={"code": "SESSION_NOT_FOUND"}) from None
        try:
            # The V1 session-create request and V2 thread-create request may
            # race. Bind even when the session already exists; otherwise an
            # unassigned session is incorrectly forced onto Python control.
            await _bind_new_assignment(request, user, session_id)
        except Exception as exc:
            if created_here:
                await session_manager.delete(session_id)
            raise HTTPException(
                status_code=409,
                detail={"code": "AGENT_RUNTIME_ASSIGNMENT_CONFLICT"},
            ) from exc
    else:
        session = await session_manager.create(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            service_id="__builtin_assistant__",
        )
        session_id = session.session_id
        created_here = True
        try:
            await _bind_new_assignment(request, user, session_id)
        except Exception:
            await session_manager.delete(session_id)
            raise
    assignments = getattr(request.app.state, "assistant_runtime_assignments", None)
    if assignments is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_ASSIGNMENT_UNAVAILABLE"})
    assignment = await _assignment(request, user, session_id)
    del assignment
    store = _store(request)
    existing = await store.get_for_session(
        tenant_id=user.tenant_id, user_id=user.user_id, session_id=session_id
    )
    control = getattr(request.app.state, "agent_runtime_control", None)
    settings = getattr(request.app.state, "settings", None)
    model_id = body.model_id or str(getattr(settings, "default_model", "") or "").strip()
    if not model_id:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_MODEL_UNAVAILABLE"})
    if existing:
        if control is None:
            raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_UNAVAILABLE"})
        await control.verify_thread(
            runtime_thread_id=existing.runtime_thread_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
            model_id=model_id,
        )
        return {"thread": _thread_payload(existing)}
    if control is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_UNAVAILABLE"})
    try:
        runtime_thread = await control.ensure_thread(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
            model_id=model_id,
        )
    except AgentRuntimeControlError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
        ) from exc
    kernel_thread_id = str(runtime_thread["runtime_thread_id"])
    # Import legacy history against the Runtime-authorized root so resume
    # hydrates the real Agent ThreadStore instead of creating an orphan UUID.
    history = await session_manager.history(session_id, limit=1)
    if history:
        thread = await store.import_legacy(
            tenant_id=user.tenant_id, user_id=user.user_id, session_id=session_id,
            runtime_thread_id=kernel_thread_id,
        )
    else:
        thread = await store.ensure_native(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
            runtime_thread_id=kernel_thread_id,
        )
    return {"thread": _thread_payload(thread)}


async def _get_thread(request: Request, user: UserContext, thread_id: str) -> RuntimeThread:
    thread = await _store(request).get(
        tenant_id=user.tenant_id, user_id=user.user_id, runtime_thread_id=thread_id
    )
    if thread is None:
        raise HTTPException(status_code=404, detail={"code": "THREAD_NOT_FOUND"})
    await _assignment(request, user, thread.session_id)
    return thread


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request, user: UserContext = Depends(get_user_context)) -> dict[str, Any]:
    _require_actor(user)
    return {"thread": _thread_payload(await _get_thread(request, user, thread_id))}


@router.post("/threads/{thread_id}/turns", status_code=202)
async def create_turn(
    thread_id: str,
    body: TurnCreateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    _require_actor(user)
    _reject_unmigrated_turn_capabilities(body)
    thread = await _get_thread(request, user, thread_id)
    control = getattr(request.app.state, "agent_runtime_control", None)
    if control is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_UNAVAILABLE"})
    settings = getattr(request.app.state, "settings", None)
    model_id = body.model_id or str(getattr(settings, "default_model", "") or "").strip()
    if not model_id:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_MODEL_UNAVAILABLE"})
    try:
        turn = await control.start_turn(
            tenant_id=user.tenant_id, user_id=user.user_id, session_id=thread.session_id,
            message=body.message, model_id=model_id,
            reasoning_option=body.reasoning_option,
            legacy_thinking_level=body.thinking_level,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            memory_mode=body.memory_mode,
            readonly_capabilities={
                "knowledge": {
                    "dataset_ids": body.kb_dataset_ids,
                    "mode": body.kb_mode,
                    "top_k": body.kb_top_k,
                    "score_threshold": body.kb_score_threshold,
                },
                "attachments": {"refs": body.file_paths},
                "web_search": {
                    "enabled": body.web_search_enabled,
                    "max_results": body.web_search_max_results,
                },
            },
        )
    except Exception as exc:
        if hasattr(exc, "code"):
            logger.warning(
                "Agent Runtime turn rejected code=%s status=%s",
                getattr(exc, "code", "AGENT_RUNTIME_ERROR"),
                int(getattr(exc, "status_code", 503)),
            )
            raise HTTPException(status_code=int(getattr(exc, "status_code", 503)), detail={"code": exc.code}) from exc
        raise
    return {
        "schema_version": "agent-turn/v2",
        "turn": {
            "id": turn.run_id,
            "thread_id": thread.runtime_thread_id,
            "status": "in_progress",
            "requested_reasoning_option": turn.requested_reasoning_option,
            "effective_reasoning_option": turn.effective_reasoning_option,
            "events_url": f"/api/v2/agent/threads/{thread.runtime_thread_id}/events?after_sequence={turn.after_sequence}&turn_id={turn.run_id}",
        },
    }


@router.post("/threads/{thread_id}/turns/{turn_id}:interrupt")
async def interrupt_turn(
    thread_id: str,
    turn_id: str,
    body: InterruptRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    _require_actor(user)
    thread = await _get_thread(request, user, thread_id)
    control = getattr(request.app.state, "agent_runtime_control", None)
    interrupt = getattr(control, "interrupt_turn", None)
    if interrupt is None:
        raise HTTPException(status_code=501, detail={"code": "AGENT_RUNTIME_INTERRUPT_UNAVAILABLE"})
    try:
        await interrupt(
            runtime_thread_id=thread.runtime_thread_id,
            turn_id=turn_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=thread.session_id,
            reason=body.reason,
        )
    except AgentRuntimeControlError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
        ) from exc
    return {"schema_version": "agent-turn/v2", "turn_id": turn_id, "status": "interrupt_requested"}


@router.get("/threads/{thread_id}/approvals/{approval_id}")
async def get_thread_approval(
    thread_id: str,
    approval_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    """Read a pending approval through the owning Agent Runtime.

    The thread lookup is deliberately performed before forwarding the request;
    this keeps approval IDs from becoming a cross-tenant oracle and binds the
    Runtime scope to the session that owns the thread.
    """
    _require_actor(user)
    thread = await _get_thread(request, user, thread_id)
    control = getattr(request.app.state, "agent_runtime_control", None)
    if control is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_UNAVAILABLE"})
    try:
        approval = await control.get_approval(
            approval_id=approval_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=thread.session_id,
        )
    except AgentRuntimeControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail={"code": "APPROVAL_NOT_FOUND"})
    return {"schema_version": "agent-approval/v2", "approval": approval}


@router.post("/threads/{thread_id}/approvals/{approval_id}/decision")
async def decide_thread_approval(
    thread_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> dict[str, Any]:
    """Consume one Runtime approval decision, preserving tenant/thread scope."""
    _require_actor(user)
    thread = await _get_thread(request, user, thread_id)
    control = getattr(request.app.state, "agent_runtime_control", None)
    if control is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_UNAVAILABLE"})
    try:
        result = await control.decide_approval(
            approval_id=approval_id,
            approved=body.approved,
            reason=body.reason,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=thread.session_id,
        )
    except AgentRuntimeControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    return {
        "schema_version": "agent-approval/v2",
        "approval": {
            **result,
            "approval_id": result.get("approval_id", approval_id),
            "status": result.get("status", "approved" if body.approved else "rejected"),
            "approved": body.approved,
            "reason": body.reason,
        },
    }


@router.get("/threads/{thread_id}/events")
async def thread_events(
    thread_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    turn_id: UUID | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> StreamingResponse:
    _require_actor(user)
    thread = await _get_thread(request, user, thread_id)

    control = getattr(request.app.state, "agent_runtime_control", None)
    if control is None:
        raise HTTPException(status_code=503, detail={"code": "AGENT_RUNTIME_UNAVAILABLE"})
    turn_id_value = str(turn_id) if turn_id else None
    turn_metadata = (
        await _store(request).turn_metadata(
            tenant_id=user.tenant_id, user_id=user.user_id,
            session_id=thread.session_id, runtime_thread_id=thread.runtime_thread_id,
            turn_id=turn_id_value,
        )
        if turn_id_value
        else None
    )

    async def stream() -> AsyncIterator[bytes]:
        async for raw in control.stream_thread_events(
            runtime_thread_id=thread.runtime_thread_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=thread.session_id,
            after_sequence=after_sequence,
            limit=limit,
            turn_id=turn_id_value,
        ):
            payload = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            if raw.get("event_type") == "run_started" and turn_metadata:
                payload = {**payload, **turn_metadata}
                raw = {**raw, "data": payload}
            sequence = int(raw.get("sequence") or after_sequence + 1)
            timestamp = raw.get("timestamp") or datetime.now(timezone.utc).isoformat()
            if isinstance(timestamp, (int, float)):
                timestamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            event = {
                "schema_version": "agent-event/v2",
                "thread_id": thread.runtime_thread_id,
                "sequence": sequence,
                "event": {
                    "id": str(raw.get("event_id") or f"runtime:{sequence}"),
                    "key": str(raw.get("event_key") or f"runtime:{sequence}"),
                    "type": str(raw.get("event_type") or "item"),
                    "item_id": payload.get("item_id"),
                    "turn_id": payload.get("run_id") or turn_id_value,
                    "status": payload.get("status"),
                    "payload": raw,
                },
                "timestamp": str(timestamp),
            }
            yield f"id: {sequence}\nevent: item\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


__all__ = ["router"]
