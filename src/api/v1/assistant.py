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
- GET /assistant/artifacts/{artifact_id} - Get artifact metadata
- GET /assistant/artifacts/{artifact_id}/download - Download artifact file
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..deps import get_user_context, get_knowledge_service
from ..schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConfigResponse,
    DatasetInfoResponse,
    DatasetsListResponse,
    ModelInfoResponse,
    ModelsListResponse,
    ImageGenerationRequest,
    ImageGenerationResponse,
    GeneratedImage,
)
from ..schemas.artifacts import ArtifactInfo, ArtifactListResponse, ArtifactCreateRequest
from ...core.auth.user_resolver import UserContext
from ...services.storage import get_artifact_storage
from ...services.assistant import AssistantService, AssistantConfig, ModelRegistry, ModelProvider
from ...services.assistant.assistant_service import RAGMode
from ...services.knowledge.embedding import is_multimodal_embedding_model


router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger(__name__)


# =========================================================================
# Session Management Schemas
# =========================================================================

class SessionCreateRequest(BaseModel):
    """Request to create a new assistant session."""
    metadata: Optional[dict] = None  # Optional metadata like title


class SessionResponse(BaseModel):
    """Response with session info."""
    session_id: str
    user_id: str
    tenant_id: str
    service_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: Optional[dict] = None
    message_count: int = 0


class SessionListResponse(BaseModel):
    """Response with list of sessions."""
    sessions: List[SessionResponse]
    total: int


class SessionHistoryMessage(BaseModel):
    """A message in session history."""
    role: str
    content: str
    timestamp: Optional[str] = None
    metadata: Optional[dict] = None


class SessionHistoryResponse(BaseModel):
    """Response with session history."""
    session_id: str
    messages: List[SessionHistoryMessage]
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


def _user_can_access_model(user: UserContext, access_level: str) -> bool:
    """
    Check if a user can access a model based on access level.

    Access levels:
    - public: All authenticated users
    - premium: Users with tier=premium/enterprise/admin or role=admin
    - admin: Only users with tier=admin or role=admin
    """
    from ...services.assistant.model_registry import ModelAccessLevel

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
        m for m in all_models
        if _user_can_access_model(user, m.access_level.value)
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
    if not kb_service:
        return DatasetsListResponse(datasets=[])

    try:
        # list_datasets expects UserContext, returns List[Dict[str, Any]]
        datasets_raw = await kb_service.list_datasets(user=user)

        datasets = []
        for ds in datasets_raw:
            dataset_id = ds.get("dataset_id", "")
            document_count = 0
            chunk_count = 0
            embedding_model = ds.get("embedding_model", "")

            # Fetch statistics for each dataset
            try:
                stats = await kb_service.get_dataset_statistics(user, dataset_id)
                document_count = stats.get("document_count", 0)
                chunk_count = stats.get("segment_count", 0)  # segments = chunks
            except Exception:
                pass  # Keep defaults if stats fetch fails

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
        p.value for p in ModelProvider
        if model_registry.is_provider_configured(p)
    ]

    kb_service = getattr(request.app.state, "knowledge_service", None)
    tavily_api_key = os.getenv("TAVILY_API_KEY")

    # Get available tools
    tool_registry = getattr(request.app.state, "tool_registry", None)
    tools_available = []
    if tool_registry:
        tools_available = [t.name for t in tool_registry.list_tools()]

    return AssistantConfigResponse(
        default_model_id="gemini-3-flash-preview",
        available_providers=available_providers,
        kb_enabled=kb_service is not None,
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
    when_to_use: Optional[str] = None
    when_not_to_use: Optional[str] = None


class ToolsListResponse(BaseModel):
    """Response for listing available tools."""
    tools: List[ToolInfoResponse]


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


def _check_model_permission(user: UserContext, model_id: str, model_registry: ModelRegistry) -> None:
    """Check if user has permission to use the specified model."""
    model = model_registry.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")

    if not _user_can_access_model(user, model.access_level.value):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: Model '{model_id}' requires {model.access_level.value} access level"
        )


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
    # Check model permission
    model_registry = getattr(request.app.state, "model_registry", None)
    if model_registry:
        _check_model_permission(user, body.model_id, model_registry)

    session_id = body.session_id or str(uuid.uuid4())

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
    )

    # Convert history to dict format, or None to trigger auto-load from session
    history = [{"role": m.role, "content": m.content} for m in body.history] if body.history else None

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
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")


@router.post("/chat/stream")
async def chat_stream(
    body: AssistantChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    assistant: AssistantService = Depends(get_assistant_service),
):
    """
    Streaming chat completion (SSE).

    Returns Server-Sent Events with incremental response chunks.
    Event types:
    - context_retrieved: KB search results
    - text_delta: Incremental text content
    - tool_call: Tool invocation
    - usage: Token usage statistics
    - done: Stream completion
    - error: Error occurred
    """
    # Check model permission
    model_registry = getattr(request.app.state, "model_registry", None)
    if model_registry:
        _check_model_permission(user, body.model_id, model_registry)

    session_id = body.session_id or str(uuid.uuid4())

    # Debug: Log incoming request parameters
    logger.info(
        f"chat_stream request - kb_dataset_ids: {body.kb_dataset_ids}, "
        f"kb_mode: {body.kb_mode}, model: {body.model_id}"
    )

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
    )

    # Convert history to dict format, or None to trigger auto-load from session
    history = [{"role": m.role, "content": m.content} for m in body.history] if body.history else None

    async def event_generator():
        """Generate SSE events."""
        try:
            async for event in assistant.chat_stream(
                user=user,
                session_id=session_id,
                message=body.message,
                config=config,
                history=history,
            ):
                # Format as SSE
                event_data = {
                    "event_type": event.event_type,
                    "data": event.data,
                    "timestamp": event.timestamp,
                }
                yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.error(f"Stream error: {e}")
            error_data = {
                "event_type": "error",
                "data": {"message": str(e)},
                "timestamp": time.time(),
            }
            yield f"data: {json.dumps(error_data)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
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


async def _list_assistant_sessions(session_manager, user: UserContext, limit: int):
    """List assistant sessions with legacy service_id compatibility."""
    primary = await session_manager.list_sessions(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
        limit=limit,
    )
    legacy = await session_manager.list_sessions(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="assistant",
        limit=limit,
    )

    merged = {}
    for session in list(primary) + list(legacy):
        existing = merged.get(session.session_id)
        if existing is None:
            merged[session.session_id] = session
            continue
        existing_ts = existing.updated_at or existing.created_at or datetime.min
        candidate_ts = session.updated_at or session.created_at or datetime.min
        if candidate_ts > existing_ts:
            merged[session.session_id] = session

    sessions = list(merged.values())
    sessions.sort(
        key=lambda s: s.updated_at or s.created_at or datetime.min,
        reverse=True,
    )
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
        sessions = await _list_assistant_sessions(session_manager, user, limit)

        return SessionListResponse(
            sessions=[
                SessionResponse(
                    session_id=s.session_id,
                    user_id=s.user_id,
                    tenant_id=s.tenant_id,
                    service_id=s.service_id,
                    created_at=s.created_at.isoformat() if s.created_at else None,
                    updated_at=s.updated_at.isoformat() if s.updated_at else None,
                    metadata=s.metadata,
                    message_count=len(s.history) if s.history else 0,
                )
                for s in sessions
            ],
            total=len(sessions),
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
        mime_type = mime_type_map.get(body.format.lower(), "application/octet-stream")

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
    Generate images with smart routing based on current model provider.

    Routing logic:
    - DashScope models (Qwen) → Aliyun Wanx API
    - Google models (Gemini) → Gemini Native Image (gemini-2.5-flash-image)
    - Other providers → Fallback to DashScope Wanx
    """
    import time

    start_time = time.time()

    # Get model to determine provider
    model = model_registry.get_model(body.model_id)
    if not model:
        # Default to DashScope if model not found
        provider_name = "dashscope"
    else:
        provider_name = model.provider.value

    # Determine which image generator to use
    use_gemini = provider_name == "google"

    logger.info(f"Image generation request - provider: {provider_name}, use_gemini: {use_gemini}, prompt: {body.prompt[:50]}...")

    try:
        if use_gemini:
            # Use Gemini Native Image (Nano Banana)
            from ...services.assistant.tools.gemini_image_tool import get_gemini_image_generator

            generator = get_gemini_image_generator()
            if not generator.is_configured:
                # Fallback to DashScope if Gemini not configured
                logger.warning("Gemini API not configured, falling back to DashScope")
                use_gemini = False
            else:
                result = await generator.generate(
                    prompt=body.prompt,
                    n=body.n,
                )

                if not result.success:
                    return ImageGenerationResponse(
                        success=False,
                        images=[],
                        provider="google",
                        duration_ms=result.duration_ms,
                        error=result.error,
                    )

                # Convert to response format
                images = [
                    GeneratedImage(
                        url=f"data:{img.get('mime_type', 'image/png')};base64,{img.get('content_base64', '')}",
                        width=1024,  # Gemini default
                        height=1024,
                    )
                    for img in result.images
                ]

                return ImageGenerationResponse(
                    success=True,
                    images=images,
                    provider="google",
                    duration_ms=result.duration_ms,
                )

        # Use DashScope Wanx (default or fallback)
        from ...services.assistant.tools.image_generator_tool import get_image_generator

        generator = get_image_generator()
        if not generator.is_configured:
            return ImageGenerationResponse(
                success=False,
                images=[],
                provider="dashscope",
                duration_ms=(time.time() - start_time) * 1000,
                error="DashScope API not configured",
            )

        # Map style
        style_map = {
            "default": "<auto>",
            "auto": "<auto>",
            "photography": "<photography>",
            "portrait": "<portrait>",
            "3d": "<3d cartoon>",
            "anime": "<anime>",
            "oil": "<oil painting>",
            "watercolor": "<watercolor>",
            "sketch": "<sketch>",
            "flat": "<flat illustration>",
        }
        style = style_map.get(body.style or "default", "<auto>")

        result = await generator.generate(
            prompt=body.prompt,
            size=body.size or "1024*1024",
            style=style,
            n=body.n,
        )

        if not result.success:
            return ImageGenerationResponse(
                success=False,
                images=[],
                provider="dashscope",
                duration_ms=result.duration_ms,
                error=result.error,
            )

        # Parse size to get dimensions
        width, height = 1024, 1024
        if body.size:
            try:
                parts = body.size.split("*")
                if len(parts) == 2:
                    width, height = int(parts[0]), int(parts[1])
            except ValueError:
                pass

        images = []
        for img in result.images:
            mime_type = img.get('mime_type', 'image/png')
            content_base64 = img.get('content_base64', '')
            size_bytes = img.get('size_bytes', 0)
            data_url = f"data:{mime_type};base64,{content_base64}"

            logger.debug(f"Image data: mime={mime_type}, base64_len={len(content_base64)}, size_bytes={size_bytes}")

            images.append(GeneratedImage(
                url=data_url,
                width=width,
                height=height,
            ))

        return ImageGenerationResponse(
            success=True,
            images=images,
            provider="dashscope",
            duration_ms=result.duration_ms,
        )

    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        return ImageGenerationResponse(
            success=False,
            images=[],
            provider=provider_name,
            duration_ms=(time.time() - start_time) * 1000,
            error=str(e),
        )
