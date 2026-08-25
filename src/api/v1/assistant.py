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
import inspect
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from ai_gateway_core.exceptions import SessionAlreadyExistsError
from ai_gateway_core.knowledge import is_multimodal_embedding_model
from ai_gateway_core.logging import record_internal_exception
from ai_gateway_core.storage import get_artifact_storage
from ai_gateway_core.style_presets import StylePreset  # noqa: F401 — pydantic schema uses it
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from ...core.assistant_capability_catalog import (
    AssistantCapabilityCatalogError,
    load_assistant_capability_catalog,
    load_gateway_assistant_policies,
    project_assistant_tools,
)
from ...core.auth.user_resolver import UserContext
from ...services.agent_runtime.thread_store import AgentThreadStore
from ..deps import get_user_context
from ..schemas.artifacts import ArtifactCreateRequest, ArtifactInfo, ArtifactListResponse
from ..schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConfigResponse,
    DatasetsListResponse,
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


def _assistant_model_service(request: Request) -> Any:
    """Return the gateway-owned model service for Assistant read routes."""

    model_service = getattr(request.app.state, "model_service", None)
    if model_service is not None:
        return model_service
    # Keep lightweight app fixtures and early-startup callers compatible with
    # the model facade while the canonical application state remains
    # ``model_service``.
    model_meta = getattr(request.app.state, "model_meta", None)
    return getattr(model_meta, "model_service", None)


def _visible_assistant_models(user: UserContext, rows: Any) -> list[dict[str, Any]]:
    """Project enabled, tenant-scoped model rows into the public Assistant shape."""

    if not isinstance(rows, list):
        rows = list(rows or [])
    visible: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not bool(row.get("is_enabled", True)):
            continue
        row_tenant_id = row.get("tenant_id")
        if row_tenant_id is not None and str(row_tenant_id) != (user.tenant_id or "default"):
            continue
        access_level = row.get("access_level")
        # ``_user_can_access_model`` deliberately rejects malformed access
        # levels, including for administrators.
        if not isinstance(access_level, str) or not _user_can_access_model(user, access_level):
            continue
        model_id = str(row.get("model_id") or "").strip()
        provider_id = str(row.get("provider_id") or "").strip()
        effective_capabilities = row.get("effective_capabilities")
        if effective_capabilities is None:
            effective_capabilities = {}
        if not model_id or not provider_id or not isinstance(effective_capabilities, dict):
            continue
        try:
            context_window = int(row.get("context_window") or 0)
            max_output_tokens = int(row.get("max_output_tokens") or 0)
            capability_revision = int(row.get("capability_revision") or 0)
            sort_order = int(row.get("sort_order") or 0)
            input_price = float(row.get("input_price_per_1k") or 0)
            output_price = float(row.get("output_price_per_1k") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if context_window <= 0 or max_output_tokens <= 0 or capability_revision < 1:
            continue
        visible.append(
            {
                "id": model_id,
                "name": str(row.get("display_name") or model_id),
                "provider": provider_id,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                "supports_vision": bool(row.get("supports_vision", False)),
                "supports_tools": bool(row.get("supports_tools", False)),
                "access_level": access_level,
                "input_price_per_1k": input_price,
                "output_price_per_1k": output_price,
                "effective_capabilities": dict(effective_capabilities),
                "capability_revision": capability_revision,
                "_sort_order": sort_order,
            }
        )
    visible.sort(key=lambda item: (item["provider"], item["_sort_order"], item["name"], item["id"]))
    for item in visible:
        item.pop("_sort_order", None)
    return visible


async def _load_visible_assistant_models(
    request: Request, user: UserContext
) -> list[dict[str, Any]]:
    model_service = _assistant_model_service(request)
    if model_service is None or not callable(getattr(model_service, "list_models", None)):
        raise HTTPException(status_code=503, detail="Model service is unavailable")
    try:
        rows = await model_service.list_models(
            tenant_id=user.tenant_id or "default",
            include_disabled=False,
        )
    except Exception as exc:
        record_internal_exception(logger, "assistant.gateway.models.internal_failure", exc)
        raise HTTPException(status_code=503, detail="Model service is unavailable") from None
    return _visible_assistant_models(user, rows)


async def _load_assistant_tools(request: Request) -> list[str]:
    """Read an already-installed safe catalog getter without importing the runtime."""

    getter = getattr(request.app.state, "assistant_capability_catalog_getter", None)
    if getter is None:
        getter = getattr(request.app.state, "agent_runtime_capability_catalog", None)
    if getter is None:
        return []
    try:
        value = getter() if callable(getter) else getter
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, dict):
            value = value.get("tools", value.get("capabilities", []))
        if not isinstance(value, (list, tuple)):
            return []
        names: list[str] = []
        for item in value:
            name = item.get("name") if isinstance(item, dict) else item
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return list(dict.fromkeys(names))
    except Exception as exc:
        record_internal_exception(logger, "assistant.gateway.config.catalog_failure", exc)
        return []


@router.get("/models", response_model=ModelsListResponse)
async def list_models(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ModelsListResponse:
    """List the tenant-scoped model catalogue owned by Gateway."""

    return ModelsListResponse(models=await _load_visible_assistant_models(request, user))


@router.get("/datasets", response_model=DatasetsListResponse)
async def list_datasets(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> DatasetsListResponse:
    """List datasets through the Gateway-owned Knowledge proxy."""

    kb_proxy = getattr(request.app.state, "kb_proxy", None)
    if kb_proxy is None or not callable(getattr(kb_proxy, "list_datasets", None)):
        raise HTTPException(status_code=503, detail="Knowledge service is unavailable")
    try:
        raw_datasets = await kb_proxy.list_datasets(user)
    except Exception as exc:
        record_internal_exception(logger, "assistant.gateway.datasets.internal_failure", exc)
        raise HTTPException(status_code=503, detail="Knowledge service is unavailable") from None
    if not isinstance(raw_datasets, list):
        raw_datasets = []
    datasets: list[dict[str, Any]] = []
    for raw in raw_datasets:
        if not isinstance(raw, dict):
            continue
        stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
        embedding_model = raw.get("embedding_model")
        datasets.append(
            {
                "dataset_id": str(raw.get("dataset_id") or ""),
                "name": str(raw.get("name") or ""),
                "description": raw.get("description"),
                "document_count": int(
                    stats.get("document_count", raw.get("document_count", 0)) or 0
                ),
                "chunk_count": int(
                    stats.get("segment_count", raw.get("segment_count", raw.get("chunk_count", 0)))
                    or 0
                ),
                "embedding_model": embedding_model,
                "is_multimodal": is_multimodal_embedding_model(str(embedding_model or "")),
            }
        )
    return DatasetsListResponse(datasets=datasets)


@router.get("/config", response_model=AssistantConfigResponse)
async def get_config(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AssistantConfigResponse:
    """Return configuration projected from Gateway-owned control-plane state."""

    visible_models = await _load_visible_assistant_models(request, user)
    settings = getattr(request.app.state, "settings", None)
    requested_default = str(getattr(settings, "default_model", "") or "").strip()
    default_model_id = next(
        (model["id"] for model in visible_models if model["id"] == requested_default),
        visible_models[0]["id"] if visible_models else "",
    )

    model_meta = getattr(request.app.state, "model_meta", None)
    available_providers: list[str] = []
    if model_meta is not None and callable(getattr(model_meta, "is_provider_configured", None)):
        for provider_id in dict.fromkeys(
            model["provider"] for model in visible_models if model["provider"]
        ):
            try:
                if await model_meta.is_provider_configured(
                    user.tenant_id or "default", provider_id
                ):
                    available_providers.append(provider_id)
            except Exception as exc:
                # Configuration must fail closed when provider state is unavailable.
                record_internal_exception(logger, "assistant.gateway.config.provider_failure", exc)

    return AssistantConfigResponse(
        default_model_id=default_model_id,
        available_providers=available_providers,
        kb_enabled=getattr(request.app.state, "kb_proxy", None) is not None,
        web_search_enabled=True,
        tools_available=await _load_assistant_tools(request),
    )


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


@router.get("/tools", response_model=ToolsListResponse)
async def list_tools(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ToolsListResponse:
    """List the Gateway-owned, tenant-authorized declarative catalog."""

    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        _, records = load_assistant_capability_catalog()
        policies = await load_gateway_assistant_policies(request, user, records)
        tools = project_assistant_tools(user, tenant_policy=policies)
    except HTTPException:
        raise
    except AssistantCapabilityCatalogError as exc:
        record_internal_exception(logger, "assistant.gateway.tools.catalog_failure", exc)
        raise HTTPException(
            status_code=503, detail="Assistant tool catalog is unavailable"
        ) from None
    return ToolsListResponse(tools=tools)


@router.get("/policies", response_model=AssistantPoliciesResponse)
async def get_policies(
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AssistantPoliciesResponse:
    """Return the Gateway-owned, tenant-scoped policy snapshot."""

    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        _, records = load_assistant_capability_catalog()
        policies = await load_gateway_assistant_policies(request, user, records)
    except HTTPException:
        raise
    except AssistantCapabilityCatalogError as exc:
        record_internal_exception(logger, "assistant.gateway.policies.catalog_failure", exc)
        raise HTTPException(status_code=503, detail="Assistant policy is unavailable") from None
    return AssistantPoliciesResponse(policies=policies)


@router.post("/approvals/{approval_id}", response_model=ApprovalResponse)
async def approve_tool_call(
    approval_id: str,
    body: ApprovalRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ApprovalResponse:
    """Project approval state through the Gateway Agent Runtime control plane."""
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
        if run and str(run.get("engine") or "") == "agent_runtime":
            control = _agent_runtime_control(request)
            session_id = str(run.get("session_id") or "")
            from ...services.agent_runtime import AgentRuntimeControlError

            try:
                approval = await control.get_approval(
                    approval_id=approval_id,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
            except AgentRuntimeControlError as exc:
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
            except AgentRuntimeControlError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            return ApprovalResponse(
                approval={
                    **approval,
                    "status": "approved" if payload["approved"] else "rejected",
                    "approved": payload["approved"],
                    "reason": payload.get("reason"),
                }
            )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "AGENT_RUNTIME_ONLY",
            "message": "Approval state is owned by the Agent Runtime.",
        },
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
               AND engine = 'agent_runtime'
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
    raise HTTPException(status_code=404, detail="Run not found")


@router.post("/runs/{run_id}/resume", response_model=ResumeResponse)
async def prepare_run_resume(
    run_id: str,
    request: Request,
    body: ResumeRequest | None = None,
    user: UserContext = Depends(get_user_context),
) -> ResumeResponse:
    """Reject legacy resume instead of dispatching to a second AgentLoop."""
    from ..deps import enforce_rate_limit

    await enforce_rate_limit(request, user, operation="assistant_resume")
    raise HTTPException(
        status_code=409,
        detail={
            "code": "AGENT_RUNTIME_ONLY",
            "message": "Resume is owned by the Agent Runtime; use the V2 turn contract.",
        },
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
    if assignment is not None and assignment.runtime_owner != "agent_runtime":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENT_RUNTIME_ASSIGNMENT_INVALID",
                "message": "The session is not owned by the Agent Runtime",
            },
        )
    return assignment


def _agent_runtime_control(request: Request) -> Any:
    control = getattr(request.app.state, "agent_runtime_control", None)
    if control is None:
        raise HTTPException(status_code=503, detail="Agent Runtime is unavailable")
    return control


async def _ensure_agent_runtime_session(
    request: Request,
    user: UserContext,
    session_id: str,
) -> Any:
    """Create/bind a session before every V1 turn; no Python fallback exists."""
    session_manager = get_session_manager(request)
    session = await session_manager.get(session_id)
    if session is not None:
        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        try:
            await session_manager.create(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                service_id="__builtin_assistant__",
                session_id=session_id,
                fail_if_exists=True,
            )
        except SessionAlreadyExistsError:
            session = await session_manager.get(session_id)
            if (
                session is None
                or session.user_id != user.user_id
                or session.tenant_id != user.tenant_id
            ):
                raise HTTPException(status_code=404, detail="Session not found") from None

    assignment_store = getattr(request.app.state, "assistant_runtime_assignments", None)
    if assignment_store is None:
        raise HTTPException(status_code=503, detail="Agent Runtime ownership is unavailable")
    assignment = await assignment_store.resolve(
        tenant_id=user.tenant_id, user_id=user.user_id, session_id=session_id
    )
    if assignment is None:
        policy = getattr(request.app.state, "assistant_runtime_assignment_policy", None)
        if policy is not None and hasattr(assignment_store, "bind_new_session"):
            assignment = await assignment_store.bind_new_session(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                session_id=session_id,
                policy=policy,
            )
        else:
            assignment = await assignment_store.bind(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                session_id=session_id,
                runtime_owner="agent_runtime",
                kernel_revision=getattr(
                    request.app.state, "assistant_runtime_kernel_revision", None
                ),
                assignment_reason="single_kernel",
            )
    if assignment.runtime_owner != "agent_runtime":
        raise HTTPException(status_code=409, detail="Invalid Agent Runtime ownership")
    return assignment


def _require_agent_runtime_request(body: AssistantChatRequest) -> None:
    """Fail closed instead of silently dropping unmigrated capabilities."""

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
                "code": "AGENT_RUNTIME_CAPABILITY_NOT_MIGRATED",
                "message": "This capability is not available on the Agent Runtime yet",
            },
        )


def _agent_runtime_readonly_capabilities(body: AssistantChatRequest) -> dict[str, Any]:
    """Build explicit read-only references for the Agent Runtime boundary."""

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


async def _start_agent_runtime_turn(
    request: Request,
    user: UserContext,
    body: AssistantChatRequest,
    *,
    session_id: str,
    model_id: str,
):
    _require_agent_runtime_request(body)
    control = _agent_runtime_control(request)
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
            temperature=body.temperature,
            memory_mode=body.memory_mode,
            memory_profile=body.memory_profile,
            readonly_capabilities=_agent_runtime_readonly_capabilities(body),
        )
    except Exception as exc:
        from ...services.agent_runtime import AgentRuntimeControlError

        if isinstance(exc, AgentRuntimeControlError):
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": "Agent Runtime rejected the turn"},
            ) from None
        raise


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    body: AssistantChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AssistantChatResponse:
    """
    Non-streaming chat completion through the Agent Runtime control plane.

    Gateway responsibilities (defence-in-depth, mirror of /chat/stream):
      - per-user rate limit
      - model-permission check (users can only call their allowed models)
      - session-ownership check (users can only resume their own sessions)

    Model routing, tool execution and persistence remain Runtime-owned.
    """
    from ..deps import enforce_rate_limit
    from ._agent_runtime_headers import reject_client_agent_forgery

    try:
        raw_body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raw_body = {}
    reject_client_agent_forgery(
        request,
        raw_body if isinstance(raw_body, dict) else {},
    )
    await enforce_rate_limit(request, user, operation="assistant_chat")

    # Model-permission authz is enforced at the Gateway edge before Runtime.
    # makes the 403 come back fast without a proxy round-trip.
    model_id = _effective_chat_model_id(request, body.model_id)
    model_meta = getattr(request.app.state, "model_meta", None)
    if model_meta:
        await _check_model_permission(user, model_id, model_meta)

    session_id = body.session_id or str(uuid.uuid4())
    await _validate_chat_session_access(request=request, user=user, session_id=session_id)
    await _ensure_agent_runtime_session(request, user, session_id)
    started_at = time.perf_counter()
    turn = await _start_agent_runtime_turn(
        request,
        user,
        body,
        session_id=session_id,
        model_id=model_id,
    )
    control = _agent_runtime_control(request)
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


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Streaming chat completion (SSE) through the Agent Runtime.

    Gateway responsibilities (preserved from the in-process version):
      - JWT auth via ``get_user_context``
      - Per-user rate limiting (``operation="assistant_chat"``)
      - Model-permission check (users can only call models they're allowed to)
      - Session-ownership check (users can only resume their own sessions)

    Runtime failures remain explicit and the public SSE shape is preserved.
    """
    from ..deps import enforce_rate_limit

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

    from ._agent_runtime_headers import reject_client_agent_forgery

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

    # Authz 2: every V1 stream is a projection of the single Agent Runtime.
    session_id = validated_body.session_id or str(uuid.uuid4())
    await _validate_chat_session_access(request=request, user=user, session_id=session_id)
    await _ensure_agent_runtime_session(request, user, session_id)
    turn = await _start_agent_runtime_turn(
        request,
        user,
        validated_body,
        session_id=session_id,
        model_id=model_id,
    )
    control = _agent_runtime_control(request)
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
            "x-ai-agent-kernel": "agent_runtime",
        },
    )


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
    session_manager = get_session_manager(request)

    try:
        # Verify ownership against the Gateway's canonical session row before
        # tombstoning the authoritative Agent Runtime thread.
        session = await session_manager.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")

        control = _agent_runtime_control(request)
        await control.cleanup_session(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
        )

        await session_manager.delete(session_id)
        if await session_manager.get(session_id) is not None:
            raise HTTPException(status_code=503, detail="Session deletion was not completed")

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

        legacy_messages = [
            SessionHistoryMessage(
                role=m.role,
                content=m.content,
                timestamp=m.timestamp.isoformat() if m.timestamp else None,
                metadata=m.metadata,
            )
            for m in (session.history or [])
        ]
        runtime_messages: list[SessionHistoryMessage] = []
        runtime_total = 0
        database = getattr(request.app.state, "database", None)
        if database is not None:
            store = getattr(request.app.state, "agent_thread_store", None)
            if store is None:
                store = AgentThreadStore(database)
                request.app.state.agent_thread_store = store
            thread = await store.get_for_session(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                session_id=session_id,
            )
            if thread is not None:
                projected, runtime_total = await store.history_messages(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    runtime_thread_id=thread.runtime_thread_id,
                    limit=limit,
                )
                runtime_messages = [SessionHistoryMessage(**message) for message in projected]
        messages = (legacy_messages + runtime_messages)[-limit:]

        return SessionHistoryResponse(
            session_id=session_id,
            messages=messages,
            total=len(legacy_messages) + runtime_total,
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
    """Map the V1 task identifier to the owning Runtime run/turn interrupt."""

    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Agent Runtime is unavailable")
    try:
        run_uuid = uuid.UUID(task_id)
    except ValueError:
        run_uuid = None
    row = await database.fetchrow(
        """
        SELECT run_id, session_id, harness_thread_id, harness_turn_id, status
          FROM assistant_runs
         WHERE tenant_id = $1 AND user_id = $2 AND engine = 'agent_runtime'
           AND (
                 ($3::uuid IS NOT NULL AND run_id = $3::uuid)
                 OR harness_turn_id = $4
               )
         ORDER BY started_at DESC
         LIMIT 1
        """,
        user.tenant_id,
        user.user_id,
        run_uuid,
        task_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    status = str(row.get("status") or "")
    session_id = str(row.get("session_id") or "")
    if status not in {"running", "pending"}:
        return TaskCancelResponse(
            task_id=task_id,
            session_id=session_id,
            cancelled=status == "cancelled",
            message="Task is already complete",
        )
    thread_id = str(row.get("harness_thread_id") or "")
    turn_id = str(row.get("harness_turn_id") or row.get("run_id") or "")
    if not thread_id or not turn_id or not session_id:
        raise HTTPException(status_code=503, detail="Agent Runtime task mapping is unavailable")
    control = _agent_runtime_control(request)
    try:
        await control.interrupt_turn(
            runtime_thread_id=thread_id,
            turn_id=turn_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
            reason=(body.reason if body is not None else "client_interrupt"),
        )
    except Exception as exc:
        from ...services.agent_runtime import AgentRuntimeControlError

        if isinstance(exc, AgentRuntimeControlError):
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
        raise HTTPException(status_code=503, detail="Agent Runtime interrupt failed") from None
    return TaskCancelResponse(
        task_id=task_id,
        session_id=session_id,
        cancelled=True,
        message="Cancellation requested",
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
        if not session or session.user_id != user.user_id or session.tenant_id != user.tenant_id:
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
