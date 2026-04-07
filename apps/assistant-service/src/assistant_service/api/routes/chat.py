"""Chat endpoints — non-streaming and SSE streaming."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

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
    stream: bool = False


def _build_config(body: ChatRequest, model_registry):
    """Build AssistantConfig from request body."""
    from src.services.assistant.assistant_service import AssistantConfig, RAGMode
    from src.services.assistant.models.model_registry import ModelProvider

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
        execution_profile=body.execution_profile,
        memory_mode=body.memory_mode,
        os_agent_enabled=body.os_agent_enabled,
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
        raise HTTPException(500, f"Chat failed: {e}")


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
        except Exception as e:
            yield f"data: {json.dumps({'event_type': 'error', 'data': {'message': str(e)}, 'timestamp': time.time()})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
