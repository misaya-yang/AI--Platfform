"""
Assistant API - GPT-like chat experience.

Endpoints:
- POST /assistant/chat - Non-streaming chat
- POST /assistant/chat/stream - Streaming chat (SSE)
- GET /assistant/models - List available models
- GET /assistant/datasets - List available KB datasets
- GET /assistant/config - Get assistant configuration
- POST /assistant/sessions - Create new conversation session
- GET /assistant/sessions - List user's conversation sessions
- GET /assistant/sessions/{session_id} - Get session details
- DELETE /assistant/sessions/{session_id} - Delete session
- GET /assistant/sessions/{session_id}/history - Get session message history
- GET /assistant/sessions/{session_id}/metrics - Get session context metrics
- GET /assistant/artifacts/{artifact_id} - Get artifact metadata
- GET /assistant/artifacts/{artifact_id}/download - Download artifact file
- GET /assistant/metrics/tenant - Get tenant-level context metrics
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from typing import Annotated, Any
from urllib.parse import urlsplit

from ai_gateway_core.storage import get_artifact_storage
from ai_gateway_core.style_presets import StylePreset  # noqa: F401 — pydantic schema uses it
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from ...core.auth.user_resolver import UserContext
from ..deps import get_user_context
from ..schemas.artifacts import ArtifactCreateRequest, ArtifactInfo, ArtifactListResponse
from ..schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConfigResponse,
    AsyncImageGenerationRequest,
    AsyncImageTaskStatusResponse,
    AsyncImageTaskSubmitResponse,
    DatasetsListResponse,
    ImageBlobCompleteRequest,
    ImageBlobFetchUrlRequest,
    ImageBlobResponse,
    ImageBlobUploadUrlRequest,
    ImageBlobUploadUrlResponse,
    ImageGenerationRequest,
    ImageGenerationResponse,
    ModelsListResponse,
)
from ._artifact_headers import attachment_content_disposition

router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger(__name__)

def _browser_artifact_download_url(raw_url: str | None, artifact_id: str) -> str:
    """Return only browser-reachable URLs; local file paths stay server-side."""

    if raw_url and urlsplit(raw_url).scheme.lower() in {"http", "https"}:
        return raw_url
    return f"/api/v1/assistant/artifacts/{artifact_id}/download"


# =========================================================================
# Session Management Schemas
# =========================================================================


class SessionCreateRequest(BaseModel):
    """Request to create a new assistant session."""

    metadata: dict | None = None  # Optional metadata like title


class SessionResponse(BaseModel):
    """Response with session info."""

    session_id: str
    user_id: str
    tenant_id: str
    service_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict | None = None
    message_count: int = 0


class SessionListResponse(BaseModel):
    """Response with list of sessions."""

    sessions: list[SessionResponse]
    total: int


class SessionHistoryMessage(BaseModel):
    """A message in session history."""

    role: str
    content: str
    timestamp: str | None = None
    metadata: dict | None = None


class SessionHistoryResponse(BaseModel):
    """Response with session history."""

    session_id: str
    messages: list[SessionHistoryMessage]
    total: int


def _user_can_access_model(user: UserContext, access_level: str) -> bool:
    """
    Check if a user can access a model based on access level.

    Access levels:
    - public: All authenticated users
    - premium: Users with tier=premium/enterprise/admin or role=admin
    - admin: Only users with tier=admin or role=admin
    """
    from ai_gateway_core.enums import ModelAccessLevel

    try:
        required_access_level = ModelAccessLevel(access_level)
    except (TypeError, ValueError):
        # Dirty metadata must not turn a restricted model into a public one.
        return False

    # Admin users can access every *known* access level.
    if user.tier == "admin" or "admin" in user.roles:
        return True

    if required_access_level is ModelAccessLevel.PUBLIC:
        return True
    elif required_access_level is ModelAccessLevel.PREMIUM:
        return user.tier in ("premium", "enterprise", "admin")
    elif required_access_level is ModelAccessLevel.ADMIN:
        return False  # Only admins, checked above

    return False


def _is_missing_artifact_schema_error(exc: Exception) -> bool:
    """Treat uninitialized artifact storage as an empty artifact list during restore."""
    exc_type = type(exc)
    if exc_type.__module__.startswith("asyncpg") and exc_type.__name__ in {
        "InvalidSchemaNameError",
        "UndefinedTableError",
    }:
        return True

    message = str(exc).lower()
    return (
        'relation "assistant.artifacts" does not exist' in message
        or 'schema "assistant" does not exist' in message
    )


def _raise_artifact_not_found_if_schema_missing(exc: Exception) -> None:
    """Hide uninitialized artifact schema details behind the public 404 contract."""
    if _is_missing_artifact_schema_error(exc):
        logger.warning("Artifact storage schema is not initialized; treating artifact as not found")
        raise HTTPException(status_code=404, detail="Artifact not found") from None


@router.get("/models", response_model=ModelsListResponse)
async def list_models(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ModelsListResponse:
    """Thin proxy — assistant-service owns the model catalogue."""
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(request, user, path="models")


@router.get("/datasets", response_model=DatasetsListResponse)
async def list_datasets(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> DatasetsListResponse:
    """Thin proxy — assistant-service resolves KB datasets via knowledge-service."""
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(request, user, path="datasets")


@router.get("/config", response_model=AssistantConfigResponse)
async def get_config(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AssistantConfigResponse:
    """Thin proxy — assistant-service owns provider configuration."""
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(request, user, path="config")


# =========================================================================
# Tool Management Endpoints (Phase 2)
# =========================================================================


class ToolInfoResponse(BaseModel):
    """Tool information response."""

    name: str
    description: str
    category: str
    risk_level: str
    when_to_use: str | None = None
    when_not_to_use: str | None = None


class ToolsListResponse(BaseModel):
    """Response for listing available tools."""

    tools: list[ToolInfoResponse]


class AssistantPoliciesResponse(BaseModel):
    """Assistant gateway policy snapshot."""

    policies: dict


class ApprovalRequest(BaseModel):
    """Approve or reject a pending tool call."""

    approved: bool
    reason: str | None = None


class ApprovalResponse(BaseModel):
    """Approval mutation result."""

    approval: dict


class RunStatusResponse(BaseModel):
    """Assistant run status response."""

    run: dict


class ResumeRequest(BaseModel):
    """Optional approval binding for resume preparation."""

    approval_id: str | None = None
    session_id: str | None = None


class ResumeResponse(BaseModel):
    """Non-executing resume plan from the latest safe checkpoint."""

    resume: dict


class LocalNodePairingChallengeRequest(BaseModel):
    """Browser-safe input for starting (but never completing) pairing."""

    model_config = ConfigDict(extra="forbid")

    display_name_hint: str | None = Field(default=None, min_length=1, max_length=80)
    ttl_seconds: int = Field(default=180, ge=30, le=600)


class LocalNodeRevokeRequest(BaseModel):
    """Browser-safe reason attached to an owner-initiated device revocation."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=240)


LocalNodeOpaqueId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


async def _proxy_local_node_read(
    request: Request,
    user: UserContext,
    *,
    path: str,
):
    """Proxy one explicitly declared Local Node control-plane read.

    This is intentionally not a catch-all route. Host action dispatch,
    approval receipts, device event append, pairing completion, and grant
    creation must use trusted internal/device channels instead of Web auth.
    """

    from ._assistant_proxy import proxy_to_assistant_service

    upstream_path = "local-nodes" if not path else f"local-nodes/{path}"
    return await proxy_to_assistant_service(request, user, path=upstream_path)


@router.post("/local-nodes/pairing/challenges", status_code=201)
async def create_local_node_pairing_challenge(
    body: LocalNodePairingChallengeRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Start owner-bound pairing; device proof must complete it out of band."""

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = body.model_dump_json(exclude_none=True).encode("utf-8")
    return await proxy_to_assistant_service(
        request,
        user,
        path="local-nodes/pairing/challenges",
        body=body_bytes,
    )


@router.get("/local-nodes")
async def list_local_nodes(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    return await _proxy_local_node_read(request, user, path="")


@router.post("/local-nodes/{device_id}/revoke")
async def revoke_local_node(
    device_id: LocalNodeOpaqueId,
    body: LocalNodeRevokeRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Revoke an owner-bound device without exposing host action execution."""

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = body.model_dump_json(exclude_none=True).encode("utf-8")
    return await proxy_to_assistant_service(
        request,
        user,
        path=f"local-nodes/{device_id}/revoke",
        body=body_bytes,
    )


@router.get("/local-nodes/{device_id}/status")
async def get_local_node_status(
    device_id: LocalNodeOpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    return await _proxy_local_node_read(request, user, path=f"{device_id}/status")


@router.get("/local-nodes/{device_id}/capabilities")
async def get_local_node_capabilities(
    device_id: LocalNodeOpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    return await _proxy_local_node_read(request, user, path=f"{device_id}/capabilities")


@router.get("/local-nodes/{device_id}/doctor")
async def get_local_node_doctor(
    device_id: LocalNodeOpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    return await _proxy_local_node_read(request, user, path=f"{device_id}/doctor")


@router.get("/local-nodes/{device_id}/grants")
async def list_local_node_grants(
    device_id: LocalNodeOpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    return await _proxy_local_node_read(request, user, path=f"{device_id}/grants")


@router.delete("/local-nodes/{device_id}/grants/{grant_id}")
async def revoke_local_node_grant(
    device_id: LocalNodeOpaqueId,
    grant_id: LocalNodeOpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Remove a grant; new grants require the trusted device channel."""

    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(
        request,
        user,
        path=f"local-nodes/{device_id}/grants/{grant_id}",
    )


@router.get("/local-nodes/{device_id}/events")
async def list_local_node_events(
    device_id: LocalNodeOpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    return await _proxy_local_node_read(request, user, path=f"{device_id}/events")


@router.get("/tools", response_model=ToolsListResponse)
async def list_tools(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ToolsListResponse:
    """Thin proxy — assistant-service owns the tool registry."""
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(request, user, path="tools")


@router.get("/policies", response_model=AssistantPoliciesResponse)
async def get_policies(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AssistantPoliciesResponse:
    """Thin proxy — policy snapshot comes from assistant-service."""
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(request, user, path="policies")


@router.post("/approvals/{approval_id}", response_model=ApprovalResponse)
async def approve_tool_call(
    approval_id: str,
    body: ApprovalRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ApprovalResponse:
    """Thin proxy — approval state lives in ``assistant_tool_approvals``
    (ADR-004). Gateway forwards the request body to assistant-service."""
    body_bytes = await request.body()
    database = getattr(request.app.state, "database", None)
    if database is not None:
        try:
            run = await database.fetchrow(
                """
                SELECT r.engine, r.session_id
                  FROM assistant_tool_approvals AS a
                  JOIN assistant_runs AS r ON r.run_id = a.run_id
                 WHERE a.approval_id = $1::uuid
                   AND a.tenant_id = $2
                   AND a.user_id = $3
                """,
                approval_id,
                user.tenant_id,
                user.user_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resolve approval runtime owner",
                extra={"approval_id": approval_id},
                exc_info=exc,
            )
            raise HTTPException(
                status_code=503, detail="Approval runtime ownership unavailable"
            ) from exc
        if not run:
            raise HTTPException(status_code=404, detail="Approval not found")
        if run and str(run.get("engine") or "") == "codex_harness":
            control = getattr(request.app.state, "codex_runtime_control", None)
            if control is None:
                raise HTTPException(status_code=503, detail="Codex Runtime unavailable")
            session_id = str(run.get("session_id") or "")
            from ...services.codex_runtime import CodexRuntimeControlError

            try:
                approval = await control.get_approval(
                    approval_id=approval_id,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
            except CodexRuntimeControlError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            if approval is None:
                raise HTTPException(status_code=404, detail="Approval not found")
            payload = body.model_dump()
            try:
                await control.decide_approval(
                    approval_id=approval_id,
                    approved=bool(payload["approved"]),
                    reason=payload.get("reason"),
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
            except CodexRuntimeControlError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            return ApprovalResponse(
                approval={
                    **approval,
                    "status": "approved" if payload["approved"] else "rejected",
                    "approved": payload["approved"],
                    "reason": payload.get("reason"),
                }
            )
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(
        request, user, path=f"approvals/{approval_id}", body=body_bytes
    )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> RunStatusResponse:
    """Thin proxy — run state lives in ``assistant_runs`` (ADR-004)."""
    database = getattr(request.app.state, "database", None)
    try:
        parsed_run_id = uuid.UUID(run_id)
    except ValueError:
        parsed_run_id = None
    if database is not None and parsed_run_id is not None:
        row = await database.fetchrow(
            """
            SELECT run_id, tenant_id, user_id, session_id, status, engine,
                   usage, error, started_at, finished_at, updated_at,
                   harness_thread_id, harness_turn_id, kernel_revision,
                   capability_revision
              FROM assistant_runs
             WHERE run_id = $1 AND tenant_id = $2 AND user_id = $3
               AND engine = 'codex_harness'
            """,
            parsed_run_id,
            user.tenant_id,
            user.user_id,
        )
        if row is not None:
            payload = dict(row)
            usage = payload.get("usage")
            if isinstance(usage, str):
                with contextlib.suppress(json.JSONDecodeError):
                    payload["usage"] = json.loads(usage)
            return RunStatusResponse(run=payload)
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(request, user, path=f"runs/{run_id}")


@router.post("/runs/{run_id}/resume", response_model=ResumeResponse)
async def prepare_run_resume(
    run_id: str,
    request: Request,
    body: ResumeRequest | None = None,
    user: UserContext = Depends(get_user_context),
) -> ResumeResponse:
    """Thin proxy — validate checkpoint/approval state without executing tools."""
    from ..deps import enforce_rate_limit
    from ._assistant_proxy import proxy_to_assistant_service

    await enforce_rate_limit(request, user, operation="assistant_resume")
    body_bytes = await request.body()
    if not body_bytes:
        body_bytes = b"{}"
    return await proxy_to_assistant_service(
        request,
        user,
        path=f"runs/{run_id}/resume",
        body=body_bytes,
    )


async def _check_model_permission(user: UserContext, model_id: str, model_meta: Any) -> None:
    """Check if the user has permission to invoke ``model_id``.

    DB-backed via ``GatewayModelMeta``. Unknown model → 400; caller's
    tier/role insufficient → 403. The old ModelRegistry in-memory
    lookup was sync; swapping to a single DB query per chat request
    is cheap (well under 1 ms).
    """
    access_level = await model_meta.get_access_level(user.tenant_id, model_id)
    if access_level is None:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")

    if not _user_can_access_model(user, access_level):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: Model '{model_id}' requires {access_level} access level",
        )


def _effective_chat_model_id(request: Request, requested_model_id: str | None) -> str:
    """Resolve the exact model that Gateway authorizes and proxies downstream."""

    requested = str(requested_model_id or "").strip()
    if requested:
        return requested

    settings = getattr(request.app.state, "settings", None)
    default_model = str(getattr(settings, "default_model", "") or "").strip()
    if not default_model:
        raise HTTPException(status_code=503, detail="Default model is not configured")
    return default_model


def _chat_body_with_model(raw_body: Any, model_id: str) -> bytes:
    """Return the validated client body with the server-resolved model pinned."""

    payload = dict(raw_body) if isinstance(raw_body, dict) else {}
    payload["model_id"] = model_id
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _validate_chat_session_access(
    request: Request,
    user: UserContext,
    session_id: str,
) -> None:
    """Return 404 when client-provided session is not accessible by current user."""
    if not session_id:
        return

    session_manager = get_session_manager(request)
    session = await session_manager.get(session_id)
    if not session:
        return

    if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.service_id and session.service_id not in {"__builtin_assistant__", "assistant"}:
        raise HTTPException(status_code=404, detail="Session not found")


async def _session_runtime_assignment(
    request: Request,
    user: UserContext,
    session_id: str | None,
) -> Any | None:
    """Never let one session fall through to a different Agent kernel."""

    if not session_id:
        return None
    assignment_store = getattr(request.app.state, "assistant_runtime_assignments", None)
    if assignment_store is None:
        return None
    assignment = await assignment_store.resolve(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        session_id=session_id,
    )
    if (
        assignment is not None
        and assignment.runtime_owner == "codex_candidate"
        and getattr(request.app.state, "codex_runtime_control", None) is None
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CODEX_RUNTIME_UNAVAILABLE",
                "message": "This session is assigned to the Codex candidate runtime",
            },
        )
    return assignment


def _record_legacy_loop_usage(request: Request, session_id: str | None) -> None:
    counter = getattr(request.app.state, "legacy_loop_usage_counter", None)
    if counter is not None:
        counter.record(session_id)


def _require_phase2_candidate_request(body: AssistantChatRequest) -> None:
    """Fail closed instead of silently dropping non-read-only capabilities."""

    unsupported = bool(
        body.system_prompt
        or body.enable_task_planning
        or body.confirm_plan
        or body.os_agent_enabled
        or body.local_node_device_id
        or body.local_node_grant_ids
        or body.resume_run_id
        or body.resume_approval_id
    )
    if unsupported:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CODEX_RUNTIME_CAPABILITY_NOT_MIGRATED",
                "message": "This capability is not available on the Codex candidate yet",
            },
        )


def _candidate_readonly_capabilities(body: AssistantChatRequest) -> dict[str, Any]:
    """Build explicit read-only references for the Codex Runtime boundary."""

    return {
        "knowledge": {
            "dataset_ids": list(body.kb_dataset_ids),
            "mode": body.kb_mode,
            "top_k": body.kb_top_k,
            "score_threshold": body.kb_score_threshold,
        },
        "attachments": {"refs": list(body.file_paths)},
        "web_search": {
            "enabled": body.web_search_enabled,
            "max_results": body.web_search_max_results,
        },
    }


async def _start_codex_candidate_turn(
    request: Request,
    user: UserContext,
    body: AssistantChatRequest,
    *,
    session_id: str,
    model_id: str,
):
    _require_phase2_candidate_request(body)
    control = getattr(request.app.state, "codex_runtime_control", None)
    if control is None:
        raise HTTPException(status_code=503, detail="Codex runtime is unavailable")
    try:
        return await control.start_turn(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
            message=body.message,
            model_id=model_id,
            reasoning_option=body.reasoning_option,
            legacy_thinking_level=body.thinking_level,
            max_tokens=body.max_tokens,
            readonly_capabilities=_candidate_readonly_capabilities(body),
        )
    except Exception as exc:
        from ...services.codex_runtime import CodexRuntimeControlError

        if isinstance(exc, CodexRuntimeControlError):
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": "Codex runtime rejected the turn"},
            ) from None
        raise


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    body: AssistantChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AssistantChatResponse:
    """
    Non-streaming chat completion — thin proxy to assistant-service.

    Gateway responsibilities (defence-in-depth, mirror of /chat/stream):
      - per-user rate limit
      - model-permission check (users can only call their allowed models)
      - session-ownership check (users can only resume their own sessions)

    Everything else — model routing, tool execution, persistence — runs
    inside the assistant-service container.
    """
    from ..deps import enforce_rate_limit
    from ._assistant_proxy import reject_client_agent_forgery

    try:
        raw_body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raw_body = {}
    reject_client_agent_forgery(
        request,
        raw_body if isinstance(raw_body, dict) else {},
    )
    await enforce_rate_limit(request, user, operation="assistant_chat")

    # Model-permission authz. assistant-service enforces tenant-scoped
    # model lookups on its side too, but belt-and-braces at the edge
    # makes the 403 come back fast without a proxy round-trip.
    model_id = _effective_chat_model_id(request, body.model_id)
    model_meta = getattr(request.app.state, "model_meta", None)
    if model_meta:
        await _check_model_permission(user, model_id, model_meta)

    session_id = body.session_id or str(uuid.uuid4())
    assignment = None
    if body.session_id:
        await _validate_chat_session_access(request=request, user=user, session_id=session_id)
        assignment = await _session_runtime_assignment(request, user, session_id)

    if assignment is not None and assignment.runtime_owner == "codex_candidate":
        started_at = time.perf_counter()
        turn = await _start_codex_candidate_turn(
            request,
            user,
            body,
            session_id=session_id,
            model_id=model_id,
        )
        control = request.app.state.codex_runtime_control
        content_parts: list[str] = []
        async for frame in control.stream_events(
            turn=turn,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
        ):
            for line in frame.decode("utf-8", errors="ignore").splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if event.get("event_type") == "text_delta":
                    data = event.get("data")
                    if isinstance(data, dict) and isinstance(data.get("content"), str):
                        content_parts.append(data["content"])
        return AssistantChatResponse(
            content="".join(content_parts),
            usage={},
            contexts=[],
            duration_ms=(time.perf_counter() - started_at) * 1000,
            model_id=model_id,
            session_id=session_id,
            run_id=turn.run_id,
        )

    from ._assistant_proxy import proxy_to_assistant_service

    _record_legacy_loop_usage(request, session_id)
    body_bytes = _chat_body_with_model(raw_body, model_id)
    return await proxy_to_assistant_service(request, user, path="chat", body=body_bytes)


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Streaming chat completion (SSE) — HTTP proxy to assistant-service.

    Gateway responsibilities (preserved from the in-process version):
      - JWT auth via ``get_user_context``
      - Per-user rate limiting (``operation="assistant_chat"``)
      - Model-permission check (users can only call models they're allowed to)
      - Session-ownership check (users can only resume their own sessions)

    Everything else — model routing, tool execution, SSE event assembly —
    runs in the assistant-service container. Stopping assistant-service
    returns a clean 502 for this endpoint; restarting it resumes chat
    within the next request via the proxy's circuit breaker.
    """
    from ..deps import enforce_rate_limit
    from ._assistant_proxy import proxy_to_assistant_service

    await enforce_rate_limit(request, user, operation="assistant_chat")

    # Read request body ONCE. We need it for authz parsing and the proxy
    # needs the same bytes — starlette's Request.stream() is single-use.
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Keep the streaming edge contract identical to the typed non-streaming
    # route. In particular, fail before proxying fields that the downstream
    # service deliberately reserves for a future durable implementation.
    try:
        validated_body = AssistantChatRequest.model_validate(body_json)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc

    from ._assistant_proxy import reject_client_agent_forgery

    reject_client_agent_forgery(
        request,
        body_json if isinstance(body_json, dict) else {},
    )

    # Authz 1: model must be allowed for this user. Done at the edge so
    # 403 comes back without a proxy round-trip. DB-backed
    # ``GatewayModelMeta`` query (<1 ms) — the old in-memory
    # ModelRegistry lookup went away with Phase 5e's split.
    model_id = _effective_chat_model_id(request, validated_body.model_id)
    model_meta = getattr(request.app.state, "model_meta", None)
    if model_meta:
        await _check_model_permission(user, model_id, model_meta)

    # Authz 2: session ownership. Users resuming a conversation must own
    # that session. assistant-service would also reject mismatches via
    # its own session manager, but defence-in-depth belongs at the edge.
    session_id = validated_body.session_id
    assignment = None
    if session_id:
        await _validate_chat_session_access(request=request, user=user, session_id=session_id)
        assignment = await _session_runtime_assignment(request, user, session_id)

    if assignment is not None and assignment.runtime_owner == "codex_candidate":
        turn = await _start_codex_candidate_turn(
            request,
            user,
            validated_body,
            session_id=session_id,
            model_id=model_id,
        )
        control = request.app.state.codex_runtime_control
        return StreamingResponse(
            control.stream_events(
                turn=turn,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                session_id=session_id,
            ),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
                "x-ai-agent-kernel": "codex",
            },
        )

    body_bytes = _chat_body_with_model(body_json, model_id)
    _record_legacy_loop_usage(request, session_id)
    return await proxy_to_assistant_service(request, user, path="chat/stream", body=body_bytes)


# =========================================================================
# Session Management Endpoints
# =========================================================================


def get_session_manager(request: Request):
    """Get session manager from app state."""
    manager = getattr(request.app.state, "session_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Session manager is not initialized. Database may not be configured.",
        )
    return manager


async def _list_assistant_session_summaries(session_manager, user: UserContext, limit: int):
    """List assistant session summaries (lightweight, no history)."""
    return await session_manager.list_session_summaries(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_ids=["__builtin_assistant__", "assistant"],
        include_null_service_id=True,
        limit=limit,
    )


async def _list_assistant_sessions(session_manager, user: UserContext, limit: int):
    """Backward-compatible full session listing for assistant sessions."""
    sessions = []
    for service_id in ("__builtin_assistant__", "assistant"):
        sessions.extend(
            await session_manager.list_sessions(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                service_id=service_id,
                limit=limit,
                status="active",
            )
        )
    sessions.sort(key=lambda item: getattr(item, "updated_at", None), reverse=True)
    return sessions[:limit]


@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    body: SessionCreateRequest = None,
    user: UserContext = Depends(get_user_context),
    request: Request = None,
) -> SessionResponse:
    """
    Create a new assistant conversation session.

    Creates a persistent session for storing conversation history.
    Sessions are isolated by user and tenant.
    """
    session_manager = get_session_manager(request)

    try:
        session = await session_manager.create(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            service_id="__builtin_assistant__",  # 保留的 service_id，避免与用户注册的服务冲突
            metadata=body.metadata if body else None,
        )
        assignment_store = getattr(request.app.state, "assistant_runtime_assignments", None)
        if assignment_store is not None:
            try:
                policy = getattr(request.app.state, "assistant_runtime_assignment_policy", None)
                if policy is not None and hasattr(assignment_store, "bind_new_session"):
                    await assignment_store.bind_new_session(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        session_id=session.session_id,
                        policy=policy,
                    )
                else:
                    await assignment_store.bind(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        session_id=session.session_id,
                        runtime_owner=request.app.state.assistant_runtime_default_owner,
                        kernel_revision=request.app.state.assistant_runtime_kernel_revision,
                    )
            except Exception:
                await session_manager.delete(session.session_id)
                raise

        return SessionResponse(
            session_id=session.session_id,
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            service_id=session.service_id,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
            metadata=session.metadata,
            message_count=len(session.history) if session.history else 0,
        )
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create session: {str(e)}")


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_user_context),
    request: Request = None,
) -> SessionListResponse:
    """
    List user's assistant conversation sessions.

    Returns sessions sorted by most recently updated.
    Sessions are filtered by user and tenant for isolation.
    """
    session_manager = get_session_manager(request)

    try:
        summaries = await _list_assistant_session_summaries(session_manager, user, limit)

        return SessionListResponse(
            sessions=[
                SessionResponse(
                    session_id=s.get("session_id"),
                    user_id=s.get("user_id"),
                    tenant_id=s.get("tenant_id"),
                    service_id=s.get("service_id"),
                    created_at=s.get("created_at"),
                    updated_at=s.get("updated_at"),
                    metadata=s.get("metadata"),
                    message_count=0,  # not computed in summary path; use /history if needed
                )
                for s in summaries
            ],
            total=len(summaries),
        )
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list sessions: {str(e)}")


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    user: UserContext = Depends(get_user_context),
    request: Request = None,
) -> SessionResponse:
    """
    Get assistant session details.

    Returns session metadata and message count.
    User isolation is enforced.
    """
    session_manager = get_session_manager(request)

    try:
        session = await session_manager.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Enforce user isolation
        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")

        return SessionResponse(
            session_id=session.session_id,
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            service_id=session.service_id,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
            metadata=session.metadata,
            message_count=len(session.history) if session.history else 0,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get session: {str(e)}")


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: UserContext = Depends(get_user_context),
    request: Request = None,
):
    """
    Delete an assistant session.

    Permanently deletes the session and all its message history.
    User isolation is enforced.
    """
    from ._route_flags import proxied

    session_manager = get_session_manager(request)

    try:
        # Verify ownership against the gateway's canonical session row before
        # asking assistant-service to clear its runtime/session-schema state.
        session = await session_manager.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")

        if proxied("SESSIONS"):
            from ._assistant_proxy import proxy_to_assistant_service

            upstream_response = await proxy_to_assistant_service(
                request,
                user,
                path=f"sessions/{session_id}",
            )
            if getattr(upstream_response, "status_code", 200) >= 400:
                return upstream_response

        await session_manager.delete(session_id)
        if await session_manager.get(session_id) is not None:
            raise HTTPException(status_code=503, detail="Session deletion was not completed")

        if proxied("SESSIONS"):
            return upstream_response
        return {"session_id": session_id, "status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete session: {str(e)}")


@router.get("/sessions/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_user_context),
    request: Request = None,
) -> SessionHistoryResponse:
    """
    Get session message history.

    Returns the conversation messages for resuming a chat.
    Messages are returned in chronological order.
    """
    session_manager = get_session_manager(request)

    try:
        session = await session_manager.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Enforce user isolation
        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")

        # Get history with limit
        history = session.history[-limit:] if session.history else []

        return SessionHistoryResponse(
            session_id=session_id,
            messages=[
                SessionHistoryMessage(
                    role=m.role,
                    content=m.content,
                    timestamp=m.timestamp.isoformat() if m.timestamp else None,
                    metadata=m.metadata,
                )
                for m in history
            ],
            total=len(session.history) if session.history else 0,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session history: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get session history: {str(e)}")


# =========================================================================
# Task Management Endpoints (Phase 1 Optimization)
# =========================================================================


class TaskCancelRequest(BaseModel):
    """Request to cancel a running task."""

    reason: str | None = None


class TaskCancelResponse(BaseModel):
    """Response for task cancellation."""

    task_id: str
    session_id: str
    cancelled: bool
    message: str


@router.post("/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(
    task_id: str,
    request: Request,
    body: TaskCancelRequest | None = None,
    user: UserContext = Depends(get_user_context),
) -> TaskCancelResponse:
    """Authenticate at the edge and cancel in the owning Assistant process."""

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = json.dumps(
        body.model_dump(exclude_none=True) if body is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return await proxy_to_assistant_service(
        request,
        user,
        path=f"tasks/{task_id}/cancel",
        body=body_bytes,
    )


# =========================================================================
# Artifact Management Endpoints
# =========================================================================


@router.get("/sessions/{session_id}/artifacts", response_model=ArtifactListResponse)
async def list_session_artifacts(
    session_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ArtifactListResponse:
    """
    List all artifacts for a session.

    Returns artifacts created during the conversation session.
    Artifacts are loaded when switching back to a conversation.

    Args:
        session_id: Session ID to get artifacts for.

    Returns:
        ArtifactListResponse with list of artifacts.
    """
    # Verify session ownership
    session_manager = get_session_manager(request)
    try:
        session = await session_manager.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to verify session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Get artifacts
    artifact_storage = get_artifact_storage()
    if not artifact_storage:
        return ArtifactListResponse(artifacts=[], total=0)

    try:
        artifacts = await artifact_storage.get_session_artifacts(session_id, user.tenant_id)

        # Generate presigned download URLs
        artifact_list = []
        for art in artifacts:
            raw_download_url = await artifact_storage.get_presigned_download_url(art)
            download_url = _browser_artifact_download_url(
                raw_download_url,
                art.artifact_id,
            )
            artifact_list.append(
                ArtifactInfo(
                    artifact_id=art.artifact_id,
                    session_id=art.session_id,
                    type=art.type,
                    format=art.format,
                    title=art.title,
                    filename=art.filename,
                    size_bytes=art.size_bytes,
                    mime_type=art.mime_type,
                    source=art.source,
                    message_id=art.message_id,
                    download_url=download_url,
                    metadata=art.metadata,
                    created_at=art.created_at.isoformat() if art.created_at else None,
                )
            )

        return ArtifactListResponse(artifacts=artifact_list, total=len(artifact_list))
    except Exception as e:
        if _is_missing_artifact_schema_error(e):
            logger.warning(
                "Artifact storage schema is not initialized; returning empty artifact list"
            )
            return ArtifactListResponse(artifacts=[], total=0)
        logger.error(f"Failed to list artifacts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/artifacts/{artifact_id}", response_model=ArtifactInfo)
async def get_artifact(
    artifact_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ArtifactInfo:
    """
    Get artifact metadata with fresh download URL.

    Returns metadata for an artifact including a fresh presigned download URL.

    Args:
        artifact_id: Unique identifier for the artifact.

    Returns:
        ArtifactInfo with artifact metadata and download URL.

    Raises:
        404: Artifact not found.
    """
    artifact_storage = get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(status_code=503, detail="Artifact storage not initialized")

    try:
        artifact = await artifact_storage.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")

        # Verify ownership
        if artifact.tenant_id != user.tenant_id or artifact.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Artifact not found")

        # Generate fresh presigned URL
        raw_download_url = await artifact_storage.get_presigned_download_url(artifact)
        download_url = _browser_artifact_download_url(
            raw_download_url,
            artifact.artifact_id,
        )

        return ArtifactInfo(
            artifact_id=artifact.artifact_id,
            session_id=artifact.session_id,
            type=artifact.type,
            format=artifact.format,
            title=artifact.title,
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
            mime_type=artifact.mime_type,
            source=artifact.source,
            message_id=artifact.message_id,
            download_url=download_url,
            metadata=artifact.metadata,
            created_at=artifact.created_at.isoformat() if artifact.created_at else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        _raise_artifact_not_found_if_schema_missing(e)
        logger.error(f"Failed to get artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/artifacts", response_model=ArtifactInfo)
async def create_artifact(
    body: ArtifactCreateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """
    Create an artifact from base64 encoded content.

    Used for saving generated images, documents, etc. to the artifact storage.

    Args:
        body: Artifact creation request with base64 encoded content.

    Returns:
        Created artifact metadata with download URL.
    """
    import base64

    artifact_storage = get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(status_code=503, detail="Artifact storage not initialized")

    try:
        session_manager = get_session_manager(request)
        try:
            session = await session_manager.get(body.session_id)
        except Exception as exc:
            logger.error("Failed to verify artifact session ownership: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to verify session") from exc
        if (
            not session
            or session.user_id != user.user_id
            or session.tenant_id != user.tenant_id
        ):
            raise HTTPException(status_code=404, detail="Session not found")

        # Decode base64 content
        try:
            content = base64.b64decode(body.content_base64)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 content: {e}")

        # Determine MIME type
        mime_type_map = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "pdf": "application/pdf",
            "json": "application/json",
            "csv": "text/csv",
            "md": "text/markdown",
            "txt": "text/plain",
        }
        mime_type_map.get(body.format.lower(), "application/octet-stream")

        # Create artifact
        artifact = await artifact_storage.create_artifact(
            session_id=body.session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            type=body.type,
            format=body.format,
            title=body.title,
            filename=body.filename,
            content=content,
            source=body.source,
            message_id=body.message_id,
            metadata=body.metadata,
        )

        # Generate download URL
        # Use presigned URL if available (S3), otherwise standard URL
        raw_download_url = await artifact_storage.get_presigned_download_url(artifact)
        download_url = _browser_artifact_download_url(
            raw_download_url,
            artifact.artifact_id,
        )

        return ArtifactInfo(
            artifact_id=artifact.artifact_id,
            session_id=artifact.session_id,
            type=artifact.type,
            format=artifact.format,
            title=artifact.title,
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
            mime_type=artifact.mime_type,
            source=artifact.source,
            message_id=artifact.message_id,
            download_url=download_url,
            metadata=artifact.metadata,
            created_at=artifact.created_at.isoformat() if artifact.created_at else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(
    artifact_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """
    Delete an artifact.

    Removes the artifact file from storage and metadata from database.

    Args:
        artifact_id: Unique identifier for the artifact.

    Returns:
        Confirmation of deletion.

    Raises:
        404: Artifact not found.
    """
    artifact_storage = get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(status_code=503, detail="Artifact storage not initialized")

    try:
        artifact = await artifact_storage.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")

        # Verify ownership
        if artifact.tenant_id != user.tenant_id or artifact.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Artifact not found")

        await artifact_storage.delete_artifact(artifact_id)
        return {"artifact_id": artifact_id, "status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        _raise_artifact_not_found_if_schema_missing(e)
        logger.error(f"Failed to delete artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """
    Download an artifact file.

    Redirects to presigned URL or streams content directly.

    Args:
        artifact_id: Unique identifier for the artifact.

    Returns:
        Redirect to download URL or streaming response.

    Raises:
        404: Artifact not found.
    """
    artifact_storage = get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(status_code=503, detail="Artifact storage not initialized")

    try:
        artifact = await artifact_storage.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")

        # Verify ownership
        if artifact.tenant_id != user.tenant_id or artifact.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Artifact not found")

        # Get presigned URL and redirect
        download_url = await artifact_storage.get_presigned_download_url(artifact)
        if download_url and urlsplit(download_url).scheme.lower() in {"http", "https"}:
            from fastapi.responses import RedirectResponse

            return RedirectResponse(url=download_url)

        # Fallback: stream content directly
        content = await artifact_storage.download_artifact(artifact_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Artifact content not found")

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
        _raise_artifact_not_found_if_schema_missing(e)
        logger.error(f"Failed to download artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Image Generation Endpoints — Phase 5e thin proxy to assistant-service.
# Real implementation lives in
#   apps/assistant-service/src/assistant_service/api/routes/images.py
# =========================================================================


@router.post("/generate-image", response_model=ImageGenerationResponse)
async def generate_image(
    body: ImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageGenerationResponse:
    """Thin proxy — assistant-service owns image generation routing
    (Gemini / Doubao / DashScope) and multi-turn session history."""
    from ..deps import enforce_rate_limit

    await enforce_rate_limit(request, user, operation="image_generate")

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = await request.body()
    return await proxy_to_assistant_service(
        request,
        user,
        path="generate-image",
        body=body_bytes,
    )


@router.post("/generate-image-async", response_model=AsyncImageTaskSubmitResponse)
async def submit_image_generation(
    body: AsyncImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AsyncImageTaskSubmitResponse:
    """Thin proxy — background task lives in assistant-service's in-memory store."""
    from ..deps import enforce_rate_limit

    await enforce_rate_limit(request, user, operation="image_generate")

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = await request.body()
    return await proxy_to_assistant_service(
        request,
        user,
        path="generate-image-async",
        body=body_bytes,
    )


@router.get("/image-task/{task_id}", response_model=AsyncImageTaskStatusResponse)
async def get_image_task_status(
    task_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AsyncImageTaskStatusResponse:
    """Thin proxy — polls the task store in assistant-service."""
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(
        request,
        user,
        path=f"image-task/{task_id}",
    )


@router.post("/image-blobs/upload-url", response_model=ImageBlobUploadUrlResponse)
async def create_image_blob_upload_url(
    body: ImageBlobUploadUrlRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageBlobUploadUrlResponse:
    from ..deps import enforce_rate_limit

    await enforce_rate_limit(request, user, operation="image_generate")

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = await request.body()
    return await proxy_to_assistant_service(
        request,
        user,
        path="image-blobs/upload-url",
        body=body_bytes,
    )


@router.post("/image-blobs/complete", response_model=ImageBlobResponse)
async def complete_image_blob_upload(
    body: ImageBlobCompleteRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageBlobResponse:
    from ..deps import enforce_rate_limit

    await enforce_rate_limit(request, user, operation="image_generate")

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = await request.body()
    return await proxy_to_assistant_service(
        request,
        user,
        path="image-blobs/complete",
        body=body_bytes,
    )


@router.post("/image-blobs/fetch-url", response_model=ImageBlobResponse)
async def fetch_image_blob_from_url(
    body: ImageBlobFetchUrlRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageBlobResponse:
    from ..deps import enforce_rate_limit

    await enforce_rate_limit(request, user, operation="image_generate")

    from ._assistant_proxy import proxy_to_assistant_service

    body_bytes = await request.body()
    return await proxy_to_assistant_service(
        request,
        user,
        path="image-blobs/fetch-url",
        body=body_bytes,
    )


@router.get("/artifacts/{artifact_id}/download-url")
async def get_artifact_download_url(
    artifact_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Thin proxy — re-sign a presigned URL for an artifact variant.

    Query params: ``variant`` (display|raw|thumbnail, default display) and
    ``expires_in`` (60..3600, default 3600). Owner-scoped — 404 on
    cross-owner access. See assistant-service for the full contract."""
    # Query string is auto-appended by ``proxy.forward`` from request.url.query.
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(
        request,
        user,
        path=f"artifacts/{artifact_id}/download-url",
    )


@router.get("/image-sessions/{session_id}")
async def get_image_session_view(
    session_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Thin proxy — paginated turn history for an image session.

    Query params: ``limit`` (1..200, default 50), ``cursor`` (opaque),
    ``include_urls`` (bool, default false). Owner-scoped."""
    # Query string is auto-appended by ``proxy.forward`` from request.url.query.
    from ._assistant_proxy import proxy_to_assistant_service

    return await proxy_to_assistant_service(
        request,
        user,
        path=f"image-sessions/{session_id}",
    )


# =========================================================================
# Context Metrics Endpoints (Observability)
# =========================================================================


class ContextMetricsResponse(BaseModel):
    """Response with context metrics for a session."""

    session_id: str
    request_count: int
    avg_tokens: int
    avg_utilization: float
    avg_compression_ratio: float
    avg_cache_hit_rate: float
    total_tokens_used: int | None = None


class TenantMetricsResponse(BaseModel):
    """Response with aggregated tenant metrics."""

    tenant_id: str
    hours: int
    request_count: int
    unique_sessions: int
    total_tokens: int
    avg_tokens_per_request: int | None = None
    avg_utilization: float | None = None


@router.get(
    "/sessions/{session_id}/metrics",
    response_model=ContextMetricsResponse,
    summary="Get context metrics for a session",
    description="Returns aggregated context metrics for a specific session including token usage, compression, and cache performance.",
)
async def get_session_metrics(
    session_id: str,
    user: UserContext = Depends(get_user_context),
    request: Request = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Get context metrics for a session."""
    from ai_gateway_core.metrics import get_context_metrics_collector

    # Verify session ownership (security: prevent access to other users' metrics)
    session_manager = get_session_manager(request)
    session = await session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Session not found")

    collector = get_context_metrics_collector()
    stats = await collector.get_session_stats(session_id, limit=limit)

    return ContextMetricsResponse(
        session_id=session_id,
        request_count=stats.get("request_count", 0),
        avg_tokens=stats.get("avg_tokens", 0),
        avg_utilization=round(stats.get("avg_utilization", 0), 3),
        avg_compression_ratio=round(stats.get("avg_compression_ratio", 1.0), 2),
        avg_cache_hit_rate=round(stats.get("avg_cache_hit_rate", 0), 3),
        total_tokens_used=stats.get("total_tokens_used"),
    )


@router.get(
    "/metrics/tenant",
    response_model=TenantMetricsResponse,
    summary="Get aggregated metrics for tenant",
    description="Returns aggregated context metrics for the current tenant over a specified time window.",
)
async def get_tenant_metrics(
    user: UserContext = Depends(get_user_context),
    hours: int = Query(default=24, ge=1, le=168),
):
    """Get aggregated metrics for the current tenant."""
    from ai_gateway_core.metrics import get_context_metrics_collector

    collector = get_context_metrics_collector()
    stats = await collector.get_tenant_stats(user.tenant_id, hours=hours)

    return TenantMetricsResponse(
        tenant_id=user.tenant_id,
        hours=hours,
        request_count=stats.get("request_count", 0),
        unique_sessions=stats.get("unique_sessions", 0),
        total_tokens=stats.get("total_tokens", 0),
        avg_tokens_per_request=stats.get("avg_tokens_per_request"),
        avg_utilization=round(stats.get("avg_utilization", 0), 3)
        if stats.get("avg_utilization")
        else None,
    )
