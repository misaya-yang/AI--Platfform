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

import asyncio
import base64 as _b64
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...core.auth.user_resolver import UserContext
from ai_gateway_core.exceptions import PermissionDeniedError
from assistant_service.core import AssistantConfig, AssistantService, ModelProvider, ModelRegistry
from assistant_service.core.assistant_service import RAGMode
from assistant_service.core.tools.gemini_image_tool import get_gemini_image_generator
from assistant_service.core.tools.image_callback import send_image_callback
from assistant_service.core.tools.image_helpers import (
    append_image_turns,
    build_gemini_contents_from_history,
    parse_image_size,
    resolve_image_routing,
)
from assistant_service.core.tools.image_watermark import apply_watermark_b64
from assistant_service.core.tools.smart_image_generator import get_smart_image_generator
from assistant_service.core.tools.style_presets import (
    StylePreset,
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)
from ...services.knowledge.embedding import is_multimodal_embedding_model
from ...services.storage import get_artifact_storage
from ..deps import get_user_context
from ..schemas.artifacts import ArtifactCreateRequest, ArtifactInfo, ArtifactListResponse
from ..schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConfigResponse,
    DatasetInfoResponse,
    DatasetsListResponse,
    AsyncImageArtifact,
    AsyncImageGenerationRequest,
    AsyncImageTaskStatusResponse,
    AsyncImageTaskSubmitResponse,
    GeneratedImage,
    ImageGenerationRequest,
    ImageGenerationResponse,
    ModelInfoResponse,
    ModelsListResponse,
)

router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger(__name__)


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


def get_assistant_service(request: Request) -> AssistantService:
    """Get AssistantService from app state."""
    svc = getattr(request.app.state, "assistant_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Assistant service is not initialized. Check LLM provider configuration.",
        )
    return svc


def get_model_registry(request: Request) -> ModelRegistry:
    """Get ModelRegistry from app state."""
    registry = getattr(request.app.state, "model_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="Model registry is not initialized.",
        )
    return registry


_SESSION_METADATA_SOFT_CAP = 1_000_000  # leave ~48KB headroom under the 1MB DB cap


async def _upload_to_gemini_files(gemini, result_image: dict) -> str | None:
    """Upload a freshly generated image to Gemini Files API, return its URI.

    Returning None signals the caller to fall back to inline base64 for this
    turn (which will almost certainly blow the 1MB session cap and lose the
    visual anchor — but at least the current response still works). Upload
    failure is logged but never propagates: image generation already succeeded,
    and a missing URI only affects the *next* turn's editing ability, not the
    current user-facing response.
    """
    try:
        import base64 as _b64
        data_b64 = result_image.get("content_base64")
        if not data_b64:
            return None
        raw = _b64.b64decode(data_b64)
        return await gemini.upload_image(raw, result_image.get("mime_type", "image/jpeg"))
    except Exception as exc:
        logger.warning(
            "Gemini Files API upload failed; next-turn edit will lack visual anchor: %s",
            exc,
        )
        return None


def _slim_image_history(history: list[dict]) -> list[dict]:
    """Cap image_chat_history size so it fits under the 1MB session metadata limit.

    Multi-turn image history has three heavy per-turn fields, each of which can
    independently exceed 1MB:
      - ``image_base64``       — the generated image itself (~1MB for 1024x1024 JPEG)
      - ``thought_signature``  — Gemini 3.x reasoning-continuity blob (~1MB)
      - ``mime_type``          — trivial, dropped alongside image_base64

    We apply graduated degradation so the most useful context survives under the
    tightest budget:

    * **Pass 1** — keep ``image_base64`` + ``thought_signature`` only on the
      most recent model turn. Older model turns drop both so they can't add
      multiple MB each.
    * **Pass 2** — if the kept turn alone still exceeds the soft cap, drop its
      ``thought_signature`` (Gemini can still edit from the visible image).
    * **Pass 3** — if still over, drop the ``image_base64`` too. The history
      survives as text-only scaffolding instead of being silently rejected.

    Losing the whole session lineage on every turn is strictly worse than
    degrading: the user at least keeps their prior prompts, and a partial
    context is something, rather than the current "every turn looks like a
    fresh session" failure mode.
    """
    if not history:
        return history

    import json as _json

    def _strip_image(turn: dict) -> None:
        turn.pop("image_base64", None)
        turn.pop("mime_type", None)

    def _strip_signature(turn: dict) -> None:
        turn.pop("thought_signature", None)

    # Pass 1: keep image+signature only on the last model turn
    keep_count = 0
    max_keep = 1
    result: list[dict] = []
    for turn in reversed(history):
        turn = dict(turn)  # shallow copy so mutations don't leak
        if turn.get("role") == "model":
            has_heavy_payload = turn.get("image_base64") or turn.get("thought_signature")
            if has_heavy_payload:
                if keep_count < max_keep:
                    keep_count += 1
                else:
                    _strip_image(turn)
                    _strip_signature(turn)
        result.append(turn)
    result.reverse()

    # Pass 2: if signature alone pushes us over, drop it (image is more useful)
    try:
        if len(_json.dumps(result)) > _SESSION_METADATA_SOFT_CAP:
            for turn in result:
                if turn.get("role") == "model":
                    _strip_signature(turn)

            # Pass 3: image too large? drop it; text-only history still beats
            # losing the whole lineage.
            if len(_json.dumps(result)) > _SESSION_METADATA_SOFT_CAP:
                for turn in result:
                    if turn.get("role") == "model":
                        _strip_image(turn)
    except (TypeError, ValueError):
        pass

    return result


def _user_can_access_model(user: UserContext, access_level: str) -> bool:
    """
    Check if a user can access a model based on access level.

    Access levels:
    - public: All authenticated users
    - premium: Users with tier=premium/enterprise/admin or role=admin
    - admin: Only users with tier=admin or role=admin
    """
    from assistant_service.core.models.model_registry import ModelAccessLevel

    # Admin users can access everything
    if user.tier == "admin" or "admin" in user.roles:
        return True

    if access_level == ModelAccessLevel.PUBLIC.value:
        return True
    elif access_level == ModelAccessLevel.PREMIUM.value:
        return user.tier in ("premium", "enterprise", "admin")
    elif access_level == ModelAccessLevel.ADMIN.value:
        return False  # Only admins, checked above

    return True  # Default allow for unknown levels


@router.get("/models", response_model=ModelsListResponse)
async def list_models(
    user: UserContext = Depends(get_user_context),
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> ModelsListResponse:
    """
    List available LLM models.

    Returns models from all configured providers (OpenAI, Anthropic, DeepSeek, DashScope, Google).
    Only models from providers with valid API keys are returned.
    Models are filtered based on user's permission level:
    - public: Available to all authenticated users
    - premium: Available to premium/enterprise/admin users only
    - admin: Available to admin users only (e.g., expensive Google Gemini 3 models)
    """
    all_models = model_registry.get_available_models()

    # Filter models based on user's access level
    accessible_models = [
        m for m in all_models if _user_can_access_model(user, m.access_level.value)
    ]

    return ModelsListResponse(
        models=[
            ModelInfoResponse(
                id=m.id,
                name=m.name,
                provider=m.provider.value,
                context_window=m.context_window,
                max_output_tokens=m.max_output_tokens,
                supports_vision=m.supports_vision,
                supports_tools=m.supports_tools,
                access_level=m.access_level.value,
                input_price_per_1k=m.input_price_per_1k,
                output_price_per_1k=m.output_price_per_1k,
            )
            for m in accessible_models
        ]
    )


@router.get("/datasets", response_model=DatasetsListResponse)
async def list_datasets(
    user: UserContext = Depends(get_user_context),
    request: Request = None,
) -> DatasetsListResponse:
    """
    List available knowledge base datasets.

    Returns datasets the user has access to for RAG integration.
    """
    kb_service = getattr(request.app.state, "knowledge_service", None)
    kb_proxy = getattr(request.app.state, "kb_proxy", None)

    if not kb_service and not kb_proxy:
        return DatasetsListResponse(datasets=[])

    try:
        # Use local service or HTTP proxy
        if kb_service:
            datasets_raw = await kb_service.list_datasets(user=user)
        else:
            datasets_raw = await kb_proxy.list_datasets(user=user)

        datasets = []
        for ds in datasets_raw:
            dataset_id = ds.get("dataset_id", "")
            document_count = 0
            chunk_count = 0
            embedding_model = ds.get("embedding_model", "")

            # Use counts from list response — statistics sub-dict or top-level
            stats = ds.get("statistics", {})
            document_count = stats.get("document_count", ds.get("document_count", 0))
            chunk_count = stats.get("segment_count", ds.get("segment_count", ds.get("chunk_count", 0)))

            # Determine if multimodal based on embedding model
            # Uses centralized model registry from services/knowledge/embedding.py
            is_multimodal = is_multimodal_embedding_model(embedding_model)

            datasets.append(
                DatasetInfoResponse(
                    dataset_id=dataset_id,
                    name=ds.get("name", ""),
                    description=ds.get("description"),
                    document_count=document_count,
                    chunk_count=chunk_count,
                    embedding_model=embedding_model or None,
                    is_multimodal=is_multimodal,
                )
            )

        return DatasetsListResponse(datasets=datasets)
    except Exception as e:
        logger.warning(f"Failed to list datasets: {e}")
        return DatasetsListResponse(datasets=[])


@router.get("/config", response_model=AssistantConfigResponse)
async def get_config(
    model_registry: ModelRegistry = Depends(get_model_registry),
    request: Request = None,
) -> AssistantConfigResponse:
    """
    Get assistant configuration.

    Returns default settings and available features.
    """
    import os

    available_providers = [
        p.value for p in ModelProvider if model_registry.is_provider_configured(p)
    ]

    kb_service = getattr(request.app.state, "knowledge_service", None)
    kb_proxy = getattr(request.app.state, "kb_proxy", None)
    tavily_api_key = os.getenv("TAVILY_API_KEY")

    # Get available tools
    tool_registry = getattr(request.app.state, "tool_registry", None)
    tools_available = []
    if tool_registry:
        tools_available = [t.name for t in tool_registry.list_tools()]

    return AssistantConfigResponse(
        default_model_id="qwen3.6-plus",
        available_providers=available_providers,
        kb_enabled=kb_service is not None or kb_proxy is not None,
        web_search_enabled=bool(tavily_api_key),
        tools_available=tools_available,
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


@router.get("/tools", response_model=ToolsListResponse)
async def list_tools(
    user: UserContext = Depends(get_user_context),
    request: Request = None,
) -> ToolsListResponse:
    """
    List available tools for the assistant.

    Returns tools with their descriptions and usage guidance.
    """
    tool_registry = getattr(request.app.state, "tool_registry", None)
    if not tool_registry:
        return ToolsListResponse(tools=[])

    tools = tool_registry.list_tools(user=user)
    return ToolsListResponse(
        tools=[
            ToolInfoResponse(
                name=t.name,
                description=t.description,
                category=t.category.value,
                risk_level=t.risk_level.value,
                when_to_use=t.when_to_use,
                when_not_to_use=t.when_not_to_use,
            )
            for t in tools
        ]
    )


@router.get("/policies", response_model=AssistantPoliciesResponse)
async def get_policies(
    user: UserContext = Depends(get_user_context),
    assistant: AssistantService = Depends(get_assistant_service),
) -> AssistantPoliciesResponse:
    """Get assistant gateway policies and defaults."""
    _ = user  # Ensure endpoint is authenticated
    return AssistantPoliciesResponse(policies=assistant.get_gateway_policies())


@router.post("/approvals/{approval_id}", response_model=ApprovalResponse)
async def approve_tool_call(
    approval_id: str,
    body: ApprovalRequest,
    user: UserContext = Depends(get_user_context),
    assistant: AssistantService = Depends(get_assistant_service),
) -> ApprovalResponse:
    """Approve or reject a pending tool invocation."""
    approval = await assistant.approve_tool_request(
        approval_id=approval_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        approved=body.approved,
        approver_user_id=user.user_id,
        reason=body.reason,
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return ApprovalResponse(approval=approval)


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: str,
    user: UserContext = Depends(get_user_context),
    assistant: AssistantService = Depends(get_assistant_service),
) -> RunStatusResponse:
    """Get run status for current user/tenant."""
    run = await assistant.get_run_status(
        run_id=run_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunStatusResponse(run=run)


def _check_model_permission(
    user: UserContext, model_id: str, model_registry: ModelRegistry
) -> None:
    """Check if user has permission to use the specified model."""
    model = model_registry.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")

    if not _user_can_access_model(user, model.access_level.value):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: Model '{model_id}' requires {model.access_level.value} access level",
        )


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


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    body: AssistantChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    assistant: AssistantService = Depends(get_assistant_service),
) -> AssistantChatResponse:
    """
    Non-streaming chat completion.

    Sends a message and receives the complete response at once.
    Suitable for simple integrations that don't need streaming.
    """
    from ..deps import enforce_rate_limit
    await enforce_rate_limit(request, user, operation="assistant_chat")

    # Check model permission
    model_registry = getattr(request.app.state, "model_registry", None)
    if model_registry:
        _check_model_permission(user, body.model_id, model_registry)

    session_id = body.session_id or str(uuid.uuid4())
    if body.session_id:
        await _validate_chat_session_access(request=request, user=user, session_id=session_id)

    # Map string mode to enum
    kb_mode = RAGMode.AUTO
    if body.kb_mode == "tool":
        kb_mode = RAGMode.TOOL
    elif body.kb_mode == "off":
        kb_mode = RAGMode.DISABLED

    # Get model provider from registry
    model_provider = ModelProvider.OPENAI  # default fallback
    if model_registry:
        model_info = model_registry.get_model(body.model_id)
        if model_info:
            model_provider = model_info.provider

    config = AssistantConfig(
        model_provider=model_provider,
        model_id=body.model_id,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        kb_dataset_ids=body.kb_dataset_ids,
        kb_mode=kb_mode,
        kb_top_k=body.kb_top_k,
        kb_score_threshold=body.kb_score_threshold,
        kb_include_images=body.kb_include_images,
        web_search_enabled=body.web_search_enabled,
        web_search_max_results=body.web_search_max_results,
        file_paths=body.file_paths,
        system_prompt=body.system_prompt,
        enable_task_planning=body.enable_task_planning,
        confirm_plan=body.confirm_plan,
        execution_profile=body.execution_profile,
        memory_mode=body.memory_mode,
        os_agent_enabled=body.os_agent_enabled,
        openclaw_mode=body.openclaw_mode,
        queue_mode=body.queue_mode,
        context_detail=body.context_detail,
        skills_enabled=body.skills_enabled,
        memory_profile=body.memory_profile,
    )

    # Convert history to dict format, or None to trigger auto-load from session
    history = (
        [{"role": m.role, "content": m.content} for m in body.history] if body.history else None
    )

    try:
        result = await assistant.chat(
            user=user,
            session_id=session_id,
            message=body.message,
            config=config,
            history=history,
        )
        return AssistantChatResponse(
            content=result["content"],
            usage=result["usage"],
            contexts=result["contexts"],
            duration_ms=result["duration_ms"],
            model_id=result["model_id"],
            session_id=session_id,
            run_id=result.get("run_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionDeniedError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")


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

    # Authz 1: model must be allowed for this user. The proxy target
    # (assistant-service) has no access to gateway's model_registry, so
    # this check MUST run here or users could specify any model_id.
    model_id = body_json.get("model_id")
    model_registry = getattr(request.app.state, "model_registry", None)
    if model_id and model_registry:
        _check_model_permission(user, model_id, model_registry)

    # Authz 2: session ownership. Users resuming a conversation must own
    # that session. assistant-service would also reject mismatches via
    # its own session manager, but defence-in-depth belongs at the edge.
    session_id = body_json.get("session_id")
    if session_id:
        await _validate_chat_session_access(
            request=request, user=user, session_id=session_id
        )

    return await proxy_to_assistant_service(
        request, user, path="chat/stream", body=body_bytes
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
        # First verify ownership
        session = await session_manager.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found")

        await session_manager.delete(session_id)
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
    """
    Cancel a running assistant task.

    Requests cancellation of an ongoing streaming response.
    The actual cancellation may take a moment as the current
    operation completes gracefully.

    Args:
        task_id: Task ID (from run_started event)
        body: Optional cancellation reason

    Returns:
        TaskCancelResponse with cancellation status
    """
    from assistant_service.core.tasks.task_manager import get_task_manager

    task_manager = get_task_manager()

    # Get task context to verify existence
    task_ctx = await task_manager.get_task_context(task_id)
    if not task_ctx:
        raise HTTPException(status_code=404, detail="Task not found or already completed")

    # Get session to verify ownership
    session = await task_manager.get_session(task_ctx.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Task not found")

    # Security check: Only allow cancelling own tasks
    if session.user_id != user.user_id or session.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Request cancellation
    success = await task_manager.cancel_task(task_ctx.session_id, task_id)

    reason = body.reason if body else None
    logger.info(
        f"Task cancellation requested: task_id={task_id}, user={user.user_id}, reason={reason}"
    )

    return TaskCancelResponse(
        task_id=task_id,
        session_id=task_ctx.session_id,
        cancelled=success,
        message="Cancellation requested"
        if success
        else "Task already completed or not cancellable",
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
            download_url = await artifact_storage.get_presigned_download_url(art)
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
        download_url = await artifact_storage.get_presigned_download_url(artifact)

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
        download_url = await artifact_storage.get_presigned_download_url(artifact)

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
        if download_url:
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
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "Content-Length": str(len(content)),
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download artifact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Image Generation Endpoint (Smart Routing)
# =========================================================================


@router.post("/generate-image", response_model=ImageGenerationResponse)
async def generate_image(
    body: ImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> ImageGenerationResponse:
    """
    Generate or iteratively edit images.

    Two modes:
    1. **Single-turn** (no session_id): text → image, no history.
    2. **Multi-turn** (with session_id): builds full conversation history
       (including previously generated images) and sends to Gemini.
       The MODEL decides whether to edit a previous image or create something new.

    Routing: Google models → Gemini first → DashScope fallback.
    DashScope has no multi-turn support, so sessions with history always route to Gemini.
    """
    from ..deps import enforce_rate_limit
    await enforce_rate_limit(request, user, operation="image_generate")

    start_time = time.time()

    try:
        model_info = model_registry.get_model(body.model_id)
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id, selected_provider,
        )

        if body.reference_image and not prefer_gemini:
            logger.warning(
                "reference_image ignored: image edit requires Gemini "
                "(model=%s provider=%s). Falling through to fresh generation.",
                body.model_id, selected_provider,
            )

        width, height, aspect_ratio = parse_image_size(body.size)

        async def _build_data_url(img: dict) -> str:
            """Encode one generated image to a data URL (watermark off-thread)."""
            cb64 = img.get("content_base64", "")
            mt = img.get("mime_type", "image/png")
            if body.add_watermark and cb64:
                cb64, mt = await asyncio.to_thread(apply_watermark_b64, cb64)
            return f"data:{mt};base64,{cb64}"

        # ------------------------------------------------------------------
        # Multi-turn mode: rebuild Gemini contents from session history
        # ------------------------------------------------------------------
        session_mgr = getattr(request.app.state, "session_manager", None)

        # App-driven edit mode: caller sent reference_image → stateless single
        # request, no session required. Preferred for mobile apps that keep the
        # prior turn's image locally and re-submit it each edit request.
        if body.reference_image and prefer_gemini:
            gemini = get_gemini_image_generator()
            if gemini.is_configured:
                preset = body.style
                styled_prompt = compose_styled_prompt(body.prompt, preset)
                logger.info(
                    "Image edit (reference_image): model=%s, preset=%s, prompt=%s...",
                    body.model_id, preset.value, body.prompt[:50],
                )
                res = await gemini.generate(
                    prompt=styled_prompt,
                    n=body.n,
                    aspect_ratio=aspect_ratio,
                    reference_image=body.reference_image,
                )
                if res.success and res.images:
                    urls = await asyncio.gather(*[_build_data_url(img) for img in res.images])
                    return ImageGenerationResponse(
                        success=True,
                        images=[GeneratedImage(url=u, width=width, height=height) for u in urls],
                        provider="google",
                        duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                    )
                return ImageGenerationResponse(
                    success=False, images=[], provider="google",
                    duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                    error=res.error or "Image edit failed",
                )

        if body.session_id and session_mgr and prefer_gemini:
            gemini = get_gemini_image_generator()
            if not gemini.is_configured:
                return ImageGenerationResponse(
                    success=False, images=[], provider="none",
                    duration_ms=(time.time() - start_time) * 1000,
                    error="Gemini API key not configured for multi-turn image chat",
                )

            session = await session_mgr.get(body.session_id)
            image_history: list[dict] = []
            locked_preset = StylePreset.DEFAULT
            if session and session.metadata:
                image_history = session.metadata.get("image_chat_history", [])
                locked_preset = resolve_style_preset(
                    session.metadata.get("style_preset"),
                )

            # Style lock semantics: requested preset overrides the session lock
            # only when the caller sends something other than DEFAULT. This lets
            # follow-up edits inherit the original style automatically while
            # still allowing explicit style switches.
            effective_preset = (
                body.style if body.style is not StylePreset.DEFAULT else locked_preset
            )
            styled_prompt = compose_styled_prompt(body.prompt, effective_preset)

            contents = build_gemini_contents_from_history(image_history, styled_prompt)

            logger.info(
                "Image multi-turn: session=%s, turns=%d, preset=%s, prompt=%s...",
                body.session_id, len(contents), effective_preset.value, body.prompt[:50],
            )

            res = await gemini.generate_chat(
                contents=contents, n=body.n, aspect_ratio=aspect_ratio,
            )

            # Only persist the turn pair on success — skipping on failure avoids
            # dangling unanswered user prompts that would poison the next request.
            if res.success and res.images:
                # Upload the generated image to Gemini Files API so the next
                # turn can reference it via fileData URI instead of inline base64.
                # Keeps session metadata small (~50 bytes/turn vs 1MB+).
                file_uri = await _upload_to_gemini_files(gemini, res.images[0])
                # Persist the raw user prompt in history (not the styled one) —
                # style is tracked separately so future edits can re-style
                # without the modifier compounding turn after turn.
                append_image_turns(
                    image_history, body.prompt, res.images[0], res.text,
                    file_uri=file_uri,
                )
                if session_mgr and session:
                    meta = dict(session.metadata or {})
                    meta["image_chat_history"] = image_history
                    meta["style_preset"] = effective_preset.value
                    await session_mgr.update_metadata(body.session_id, meta)
            else:
                return ImageGenerationResponse(
                    success=False, images=[], provider="google",
                    duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                    error=res.error or "Image generation failed",
                )

            urls = await asyncio.gather(*[_build_data_url(img) for img in res.images])
            images = [GeneratedImage(url=u, width=width, height=height) for u in urls]

            return ImageGenerationResponse(
                success=True, images=images, provider="google",
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
            )

        # ------------------------------------------------------------------
        # Single-turn mode (no session, non-Gemini, or Gemini single-shot)
        # ------------------------------------------------------------------
        # Expand the preset into three artefacts: a styled prompt (used by all
        # providers so Gemini/Doubao see the modifier), a DashScope native tag,
        # and a DashScope negative prompt. See style_presets.py for the table.
        preset = body.style
        styled_prompt = compose_styled_prompt(body.prompt, preset)
        dashscope_tag = resolve_dashscope_style_tag(preset)
        negative_prompt = resolve_negative_prompt(preset)

        router = get_smart_image_generator()
        logger.info(
            "Image single-turn: model_id=%s, preset=%s, prompt=%s..., size=%s",
            body.model_id, preset.value, body.prompt[:50], body.size,
        )

        res = await router.generate(
            prompt=styled_prompt, n=body.n, size=body.size or "1024*1024",
            style=dashscope_tag, negative_prompt=negative_prompt,
            aspect_ratio=aspect_ratio,
            prefer_gemini=prefer_gemini, prefer_doubao=prefer_doubao,
            dashscope_model=dashscope_model,
        )

        if not res.success:
            err = res.error or "Image generation failed"
            if res.blocked and res.block_reason:
                err = f"{err} (blocked: {res.block_reason})"
            return ImageGenerationResponse(
                success=False, images=[], provider=res.provider,
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000, error=err,
            )

        urls = await asyncio.gather(*[_build_data_url(img) for img in res.images])
        images = [GeneratedImage(url=u, width=width, height=height) for u in urls]

        return ImageGenerationResponse(
            success=True, images=images, provider=res.provider,
            duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
        )

    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        return ImageGenerationResponse(
            success=False, images=[], provider="unknown",
            duration_ms=(time.time() - start_time) * 1000, error=str(e),
        )


# =========================================================================
# Async Image Generation (background task with polling)
# =========================================================================

# In-memory task store for async image generation
# Key: task_id, Value: dict with task state
_image_tasks: dict[str, dict] = {}
# Max tasks to keep (LRU eviction of completed tasks older than 1 hour)
_MAX_TASKS = 500


def _cleanup_old_tasks() -> None:
    """Remove completed/failed tasks older than 1 hour."""
    if len(_image_tasks) < _MAX_TASKS:
        return
    now = datetime.now(timezone.utc)
    to_remove = []
    for tid, task in _image_tasks.items():
        if task["status"] in ("completed", "failed"):
            created = datetime.fromisoformat(task["created_at"])
            if (now - created).total_seconds() > 3600:
                to_remove.append(tid)
    for tid in to_remove:
        _image_tasks.pop(tid, None)


async def _process_image_result(
    img: dict,
    *,
    task_id: str,
    prompt: str,
    width: int,
    height: int,
    add_watermark: bool,
    session_id: str | None,
    user: UserContext,
    index: int,
    artifact_storage,
) -> dict:
    """Watermark + save artifact for one generated image. Runs concurrently per image."""
    content_base64 = img.get("content_base64", "")
    mime_type = img.get("mime_type", "image/png")

    if add_watermark and content_base64:
        try:
            content_base64, mime_type = await asyncio.to_thread(
                apply_watermark_b64, content_base64,
            )
        except Exception as e:
            logger.warning("Watermark failed for image %d: %s", index, e)

    data_url = f"data:{mime_type};base64,{content_base64}"
    entry: dict = {"url": data_url, "width": width, "height": height}

    if artifact_storage and session_id and content_base64:
        try:
            content = _b64.b64decode(content_base64)
            ext = mime_type.split("/")[-1] or "png"
            filename = f"generated_image_{task_id[:8]}_{index + 1}.{ext}"
            artifact = await artifact_storage.create_artifact(
                session_id=session_id,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                type="image",
                format=ext,
                title=f"Generated: {prompt[:40]}...",
                filename=filename,
                content=content,
                source="image_generation",
            )
            entry["artifact_id"] = artifact.artifact_id
            entry["download_url"] = await artifact_storage.get_presigned_download_url(artifact)
        except Exception as e:
            logger.warning("Failed to save async image artifact: %s", e)

    return entry


async def _run_image_generation_task(
    task_id: str,
    body: AsyncImageGenerationRequest,
    model_registry: ModelRegistry,
    user: UserContext,
    session_manager=None,
) -> None:
    """Background coroutine that runs image generation and saves artifacts."""
    task = _image_tasks[task_id]
    task["status"] = "running"
    task["progress"] = 10

    start_time = time.time()

    try:
        model_info = model_registry.get_model(body.model_id)
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id, selected_provider,
        )

        if body.reference_image and not prefer_gemini:
            logger.warning(
                "Async: reference_image ignored: image edit requires Gemini "
                "(model=%s provider=%s). Falling through to fresh generation.",
                body.model_id, selected_provider,
            )

        width, height, aspect_ratio = parse_image_size(body.size)

        task["progress"] = 30

        # ------------------------------------------------------------------
        # App-driven edit mode: caller sent reference_image → stateless single
        # request to Gemini with the prior image attached. No session read/write,
        # no 1MB metadata concerns, works across users trivially. This is the
        # recommended pattern for mobile apps that manage their own history.
        # ------------------------------------------------------------------
        res = None
        if body.reference_image and prefer_gemini:
            gemini = get_gemini_image_generator()
            if gemini.is_configured:
                preset = body.style
                styled_prompt = compose_styled_prompt(body.prompt, preset)
                logger.info(
                    "Async image edit (reference_image): model=%s, preset=%s, prompt=%s...",
                    body.model_id, preset.value, body.prompt[:50],
                )
                res = await gemini.generate(
                    prompt=styled_prompt,
                    n=body.n,
                    aspect_ratio=aspect_ratio,
                    reference_image=body.reference_image,
                )

        # ------------------------------------------------------------------
        # Multi-turn mode: session_id + Gemini → use chat history
        # ------------------------------------------------------------------
        if res is None and body.session_id and session_manager and prefer_gemini:
            gemini = get_gemini_image_generator()
            if gemini.is_configured:
                session = await session_manager.get(body.session_id)
                if not session:
                    session = await session_manager.create(
                        user_id=user.user_id,
                        tenant_id=user.tenant_id,
                        session_id=body.session_id,
                        metadata={"image_chat_history": []},
                    )
                image_history: list[dict] = []
                locked_preset = StylePreset.DEFAULT
                if session and session.metadata:
                    image_history = session.metadata.get("image_chat_history", [])
                    locked_preset = resolve_style_preset(
                        session.metadata.get("style_preset"),
                    )

                # Inherit session's locked style when the caller sent DEFAULT;
                # an explicit non-DEFAULT preset overrides and updates the lock.
                effective_preset = (
                    body.style if body.style is not StylePreset.DEFAULT else locked_preset
                )
                styled_prompt = compose_styled_prompt(body.prompt, effective_preset)

                contents = build_gemini_contents_from_history(image_history, styled_prompt)

                logger.info(
                    "Async image multi-turn: session=%s, turns=%d, preset=%s, prompt=%s...",
                    body.session_id, len(contents), effective_preset.value, body.prompt[:50],
                )

                res = await gemini.generate_chat(
                    contents=contents, n=body.n, aspect_ratio=aspect_ratio,
                )

                # Skip history update on failure to avoid dangling user turn
                if res.success and res.images:
                    file_uri = await _upload_to_gemini_files(gemini, res.images[0])
                    append_image_turns(
                        image_history, body.prompt, res.images[0], res.text,
                        file_uri=file_uri,
                    )
                    if session_manager and session:
                        meta = dict(session.metadata or {})
                        meta["image_chat_history"] = image_history
                        meta["style_preset"] = effective_preset.value
                        await session_manager.update_metadata(body.session_id, meta)

        # ------------------------------------------------------------------
        # Single-turn fallback (no session, non-Gemini, or Gemini not configured)
        # ------------------------------------------------------------------
        if res is None:
            preset = body.style
            styled_prompt = compose_styled_prompt(body.prompt, preset)
            dashscope_tag = resolve_dashscope_style_tag(preset)
            negative_prompt = resolve_negative_prompt(preset)

            router = get_smart_image_generator()
            res = await router.generate(
                prompt=styled_prompt,
                n=body.n,
                size=body.size or "1024*1024",
                style=dashscope_tag,
                negative_prompt=negative_prompt,
                aspect_ratio=aspect_ratio,
                prefer_gemini=prefer_gemini,
                prefer_doubao=prefer_doubao,
                dashscope_model=dashscope_model,
            )

        duration_ms = (time.time() - start_time) * 1000
        task["duration_ms"] = duration_ms
        task["provider"] = res.provider

        if not res.success:
            err = res.error or "Image generation failed"
            if res.blocked and res.block_reason:
                err = f"{err} (blocked: {res.block_reason})"
            task["status"] = "failed"
            task["error"] = err
            task["progress"] = 100
            task["completed_at"] = datetime.now(timezone.utc).isoformat()
            return

        task["progress"] = 70

        # Process all images concurrently — watermark off the event loop,
        # artifact uploads in parallel.
        artifact_storage = get_artifact_storage()
        images = await asyncio.gather(
            *[
                _process_image_result(
                    img,
                    task_id=task_id,
                    prompt=body.prompt,
                    width=width,
                    height=height,
                    add_watermark=body.add_watermark,
                    session_id=body.session_id,
                    user=user,
                    index=i,
                    artifact_storage=artifact_storage,
                )
                for i, img in enumerate(res.images)
            ]
        )

        task["images"] = images
        task["status"] = "completed"
        task["progress"] = 100
        task["completed_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        logger.error("Async image generation task %s failed: %s", task_id, e)
        task["status"] = "failed"
        task["error"] = str(e)
        task["progress"] = 100
        task["duration_ms"] = (time.time() - start_time) * 1000
        task["completed_at"] = datetime.now(timezone.utc).isoformat()

    if body.callback_url:
        try:
            await send_image_callback(body.callback_url, task)
        except Exception as e:
            logger.warning("Callback to %s failed: %s", body.callback_url, e)


@router.post("/generate-image-async", response_model=AsyncImageTaskSubmitResponse)
async def submit_image_generation(
    body: AsyncImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> AsyncImageTaskSubmitResponse:
    """
    Submit an async image generation task.

    Returns a task_id immediately. Poll GET /image-task/{task_id} for status.
    When completed, images are auto-saved to artifacts (if session_id provided).
    """
    _cleanup_old_tasks()

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    _image_tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "prompt": body.prompt,
        "model_id": body.model_id,
        "provider": None,
        "images": [],
        "duration_ms": None,
        "error": None,
        "created_at": now,
        "completed_at": None,
    }

    # Launch background task
    session_mgr = getattr(request.app.state, "session_manager", None)
    asyncio.create_task(
        _run_image_generation_task(task_id, body, model_registry, user, session_manager=session_mgr)
    )

    return AsyncImageTaskSubmitResponse(
        task_id=task_id,
        status="pending",
        message="Image generation task submitted",
    )


@router.get("/image-task/{task_id}", response_model=AsyncImageTaskStatusResponse)
async def get_image_task_status(
    task_id: str,
    user: UserContext = Depends(get_user_context),
) -> AsyncImageTaskStatusResponse:
    """
    Poll the status of an async image generation task.

    Status flow: pending → running → completed / failed
    When status is 'completed', images array contains the results with artifact info.
    """
    task = _image_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    images = [
        AsyncImageArtifact(
            artifact_id=img.get("artifact_id"),
            download_url=img.get("download_url"),
            url=img["url"],
            width=img.get("width"),
            height=img.get("height"),
        )
        for img in task.get("images", [])
    ]

    return AsyncImageTaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        progress=task.get("progress", 0),
        prompt=task["prompt"],
        model_id=task["model_id"],
        provider=task.get("provider"),
        images=images,
        duration_ms=task.get("duration_ms"),
        error=task.get("error"),
        created_at=task["created_at"],
        completed_at=task.get("completed_at"),
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
    from assistant_service.core import get_context_metrics_collector

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
    from assistant_service.core import get_context_metrics_collector

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
