"""Chat endpoints — non-streaming and SSE streaming."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service, get_model_registry

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    history: list[dict[str, str]] | None = None
    model_id: str = "qwen3.5-plus"
    temperature: float = 0.7
    max_tokens: int | None = None
    system_prompt: str | None = None
    kb_dataset_ids: list[str] | None = None
    kb_mode: str | None = "auto"
    kb_top_k: int | None = None
    kb_score_threshold: float | None = None
    kb_include_images: bool | None = None
    web_search_enabled: bool = False
    web_search_max_results: int | None = None
    file_paths: list[str] | None = None
    execution_profile: str | None = None
    memory_mode: str | None = None
    os_agent_enabled: bool | None = None
    enable_task_planning: bool = False
    confirm_plan: bool = False
    openclaw_mode: str | None = None
    queue_mode: str | None = None
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None
    stream: bool = False


def _build_config(body: ChatRequest, model_registry):
    """Build AssistantConfig from request body."""
    from ...core.assistant_service import AssistantConfig, RAGMode
    from ...core.models.model_registry import ModelProvider

    kb_mode = RAGMode.AUTO
    if body.kb_mode == "tool":
        kb_mode = RAGMode.TOOL
    elif body.kb_mode == "off":
        kb_mode = RAGMode.DISABLED

    model_provider = ModelProvider.OPENAI
    if model_registry:
        mi = model_registry.get_model(body.model_id)
        if mi:
            model_provider = mi.provider

    return AssistantConfig(
        model_provider=model_provider,
        model_id=body.model_id,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        kb_dataset_ids=body.kb_dataset_ids or [],
        kb_mode=kb_mode,
        kb_top_k=body.kb_top_k or 5,
        kb_score_threshold=body.kb_score_threshold if body.kb_score_threshold is not None else 0.65,
        kb_include_images=body.kb_include_images or False,
        web_search_enabled=body.web_search_enabled,
        web_search_max_results=body.web_search_max_results or 5,
        file_paths=body.file_paths or [],
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


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Non-streaming chat completion."""
    assistant = get_assistant_service(request)
    model_registry = get_model_registry(request)
    config = _build_config(body, model_registry)
    session_id = body.session_id or str(uuid.uuid4())
    history = body.history

    try:
        result = await assistant.chat(
            user=user, session_id=session_id, message=body.message,
            config=config, history=history,
        )
        return {
            "content": result["content"],
            "usage": result.get("usage"),
            "contexts": result.get("contexts"),
            "duration_ms": result.get("duration_ms"),
            "model_id": result.get("model_id"),
            "session_id": session_id,
            "run_id": result.get("run_id"),
        }
    except Exception as e:
        import logging
        logging.getLogger("assistant-service").error(f"Chat failed: {e}", exc_info=True)
        raise HTTPException(500, "Chat request failed. Please try again.")


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """SSE streaming chat completion."""
    assistant = get_assistant_service(request)
    model_registry = get_model_registry(request)
    config = _build_config(body, model_registry)
    session_id = body.session_id or str(uuid.uuid4())
    history = body.history

    async def event_generator():
        try:
            async for event in assistant.chat_stream(
                user=user, session_id=session_id, message=body.message,
                config=config, history=history,
            ):
                event_data = {
                    "event_type": event.event_type,
                    "data": event.data,
                    "timestamp": event.timestamp,
                }
                yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("chat_stream_failed", extra={"session_id": session_id, "user_id": user.user_id})
            # Do not leak internal error details (may contain DSN, API keys, table names).
            yield f"data: {json.dumps({'event_type': 'error', 'data': {'message': 'Chat stream failed. Please try again.'}, 'timestamp': time.time()})}\n\n"

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
