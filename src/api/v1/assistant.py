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
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
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
)
from ...core.auth.user_resolver import UserContext
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


@router.get("/models", response_model=ModelsListResponse)
async def list_models(
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> ModelsListResponse:
    """
    List available LLM models.

    Returns models from all configured providers (OpenAI, Anthropic, DeepSeek, DashScope).
    Only models from providers with valid API keys are returned.
    """
    models = model_registry.get_available_models()
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
            )
            for m in models
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
        default_model_id="gpt-4o",
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


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    body: AssistantChatRequest,
    user: UserContext = Depends(get_user_context),
    assistant: AssistantService = Depends(get_assistant_service),
) -> AssistantChatResponse:
    """
    Non-streaming chat completion.

    Sends a message and receives the complete response at once.
    Suitable for simple integrations that don't need streaming.
    """
    session_id = body.session_id or str(uuid.uuid4())

    # Map string mode to enum
    kb_mode = RAGMode.AUTO
    if body.kb_mode == "tool":
        kb_mode = RAGMode.TOOL
    elif body.kb_mode == "off":
        kb_mode = RAGMode.DISABLED

    config = AssistantConfig(
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

    config = AssistantConfig(
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
            service_id="assistant",
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
        sessions = await session_manager.list_sessions(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            service_id="assistant",
            limit=limit,
        )

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
