"""Chat endpoints — non-streaming and SSE streaming."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator

from ai_gateway_core.proxy.sse_heartbeat import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    with_sse_heartbeat,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service, get_model_registry

# Tests override this attribute to shorten the heartbeat interval —
# don't inline the constant.
_SSE_HEARTBEAT_INTERVAL_S = DEFAULT_HEARTBEAT_INTERVAL_S

logger = logging.getLogger(__name__)

router = APIRouter()
_E2E_MEMORY_BY_USER: dict[str, dict[str, str]] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    history: list[dict[str, str]] | None = None
    model_id: str = "qwen3.7-plus"
    temperature: float = 0.7
    max_tokens: int | None = None
    system_prompt: str | None = None
    eval_run: bool = False
    eval_system_prompt_override: str | None = None
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
    runtime_mode: str | None = None
    queue_mode: str | None = None
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None
    resume_run_id: str | None = None
    resume_approval_id: str | None = None
    stream: bool = False


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _user_memory_key(user: UserContext) -> str:
    return f"{user.tenant_id}:{user.user_id}"


def _build_e2e_memory_stub_response(body: ChatRequest, user: UserContext) -> str | None:
    """Deterministic local-E2E memory path when no live model key is available."""
    if not _env_truthy("ASSISTANT_E2E_STUB_LLM"):
        return None

    message = body.message.strip()
    remember_match = re.search(r"我的名字是([^，,。]+)[，,]\s*我来自([^。\.]+)", message)
    memory_key = _user_memory_key(user)
    if remember_match:
        _E2E_MEMORY_BY_USER[memory_key] = {
            "name": remember_match.group(1).strip(),
            "location": remember_match.group(2).strip(),
        }
        return "已记住"

    if "还记得我的名字" in message:
        memory = _E2E_MEMORY_BY_USER.get(memory_key)
        if memory:
            return f"你的名字是{memory['name']}，你来自{memory['location']}。"

    return None


async def _stub_stream_lines(text: str) -> AsyncIterator[str]:
    payload = {"event_type": "text_delta", "data": text, "timestamp": time.time()}
    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    done = {"event_type": "done", "data": {"usage": {}}, "timestamp": time.time()}
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"


def _otel_trace_id_from_traceparent(traceparent: str | None) -> str | None:
    if not traceparent or not traceparent.startswith("00-"):
        return None
    parts = traceparent.split("-")
    if len(parts) >= 4 and parts[1]:
        return parts[1]
    return None


def _request_traceparent(request: Request) -> str | None:
    return (
        getattr(request.state, "traceparent", None)
        or request.headers.get("traceparent")
        or None
    )


def _build_config(
    body: ChatRequest,
    model_registry,
    *,
    traceparent: str | None = None,
    otel_trace_id: str | None = None,
):
    """Build AssistantConfig from request body."""
    from ...core.assistant_service import AssistantConfig, RAGMode
    from ...core.models.model_registry import ModelProvider

    kb_mode = RAGMode.AUTO
    if body.kb_mode == "tool":
        kb_mode = RAGMode.TOOL
    elif body.kb_mode == "off":
        kb_mode = RAGMode.DISABLED

    model_id = body.model_id
    model_provider = ModelProvider.OPENAI
    if model_registry:
        mi = model_registry.get_model(model_id)
        provider_configured = (
            bool(mi)
            and (
                not hasattr(model_registry, "is_provider_configured")
                or model_registry.is_provider_configured(mi.provider)
            )
        )
        if mi is None or not provider_configured:
            if body.eval_run:
                raise HTTPException(status_code=422, detail=f"Eval model unavailable: {body.model_id}")
            available = model_registry.get_available_models()
            if available:
                mi = available[0]
                model_id = mi.id
                logger.warning(
                    "chat_requested_model_unavailable_falling_back",
                    extra={
                        "requested_model_id": body.model_id,
                        "fallback_model_id": model_id,
                    },
                )
        if mi:
            model_provider = mi.provider

    return AssistantConfig(
        model_provider=model_provider,
        model_id=model_id,
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
        eval_system_prompt_override=body.eval_system_prompt_override,
        enable_task_planning=body.enable_task_planning,
        confirm_plan=body.confirm_plan,
        execution_profile=body.execution_profile,
        memory_mode=body.memory_mode,
        os_agent_enabled=body.os_agent_enabled,
        runtime_mode=body.runtime_mode,
        queue_mode=body.queue_mode,
        context_detail=body.context_detail,
        skills_enabled=body.skills_enabled,
        memory_profile=body.memory_profile,
        traceparent=traceparent,
        otel_trace_id=otel_trace_id or _otel_trace_id_from_traceparent(traceparent),
        resume_run_id=body.resume_run_id,
        resume_approval_id=body.resume_approval_id,
    )


def _validate_eval_prompt_override(body: ChatRequest, user: UserContext) -> None:
    override = body.eval_system_prompt_override
    if not body.eval_run and override is None:
        return
    if user.user_id != "eval-candidate" or user.user_type != "system":
        raise HTTPException(status_code=403, detail="Trusted eval prompt override is internal only")
    if override is None:
        return
    if not override.strip() or len(override) > 16_000:
        raise HTTPException(status_code=422, detail="Invalid trusted eval prompt override")


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Non-streaming chat completion."""
    _validate_eval_prompt_override(body, user)
    assistant = get_assistant_service(request)
    model_registry = get_model_registry(request)
    traceparent = _request_traceparent(request)
    config = _build_config(body, model_registry, traceparent=traceparent)
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
    _validate_eval_prompt_override(body, user)
    session_id = body.session_id or str(uuid.uuid4())
    stub_text = _build_e2e_memory_stub_response(body, user)
    if stub_text is not None:
        def stub_event_generator():
            return with_sse_heartbeat(
                _stub_stream_lines(stub_text),
                interval_seconds=_SSE_HEARTBEAT_INTERVAL_S,
                as_str=True,
            )

        return StreamingResponse(
            stub_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Session-Id": session_id,
            },
        )

    assistant = get_assistant_service(request)
    model_registry = get_model_registry(request)
    traceparent = _request_traceparent(request)
    config = _build_config(body, model_registry, traceparent=traceparent)
    history = body.history

    async def _agent_lines():
        """Format the agent loop's events as SSE ``data:`` lines.

        Catches generator-side exceptions, logs them with full context,
        and yields a generic error event so the FE can render a sensible
        message without leaking internal details.
        """
        try:
            async for event in assistant.chat_stream(
                user=user, session_id=session_id, message=body.message,
                config=config, history=history,
            ):
                payload = {
                    "event_type": event.event_type,
                    "data": event.data,
                    "timestamp": event.timestamp,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(
                "chat_stream_failed",
                extra={"session_id": session_id, "user_id": user.user_id},
            )
            error_payload = {
                "event_type": "error",
                "data": {"message": "Chat stream failed. Please try again."},
                "timestamp": time.time(),
            }
            yield f"data: {json.dumps(error_payload)}\n\n"

    # ``with_sse_heartbeat`` injects ``: heartbeat`` SSE comments every
    # 15s of producer silence so long tool calls (Gemini image gen 60s+,
    # KB queries 30s+) don't trip nginx / ALB / NAT idle timeouts. The
    # helper is the canonical implementation; this route used to inline
    # the same pattern (deduped 2026-04-28).
    def event_generator():
        return with_sse_heartbeat(
            _agent_lines(),
            interval_seconds=_SSE_HEARTBEAT_INTERVAL_S,
            as_str=True,
        )

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
