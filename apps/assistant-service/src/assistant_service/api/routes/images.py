"""Image generation endpoints — sync + async + polling.

Owned by assistant-service; gateway forwards ``/api/v1/assistant/generate-image``,
``/api/v1/assistant/generate-image-async``, and
``/api/v1/assistant/image-task/{task_id}`` here via the shared ServiceProxy.

This file is the authoritative implementation. Historically these routes
lived in the gateway's ``src/api/v1/assistant.py`` because ModelRegistry
and the per-provider image generators ran in-process; Phase 5e moved the
real code here so the gateway Dockerfile can drop ``COPY apps/assistant-service``.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.image import (
    append_image_turns,
    apply_watermark_b64,
    build_gemini_contents_from_history,
    parse_image_size,
    resolve_image_routing,
    send_image_callback,
)
from ai_gateway_core.style_presets import (
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)

from ...auth import UserContext, get_user_context
from ...core.models.model_registry import ModelRegistry
from ...core.tools.gemini_image_tool import get_gemini_image_generator
from ...core.tools.smart_image_generator import get_smart_image_generator
from ..deps import get_model_registry, get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------------
# Schemas — kept compatible with gateway's public contract
# -----------------------------------------------------------------------------

class GeneratedImage(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    model_id: str = "qwen-image-2.0"
    n: int = Field(1, ge=1, le=4)
    size: str | None = "1024*1024"
    style: StylePreset = Field(default=StylePreset.DEFAULT)
    session_id: str | None = None
    reference_image: str | None = None
    add_watermark: bool = True

    @field_validator("style", mode="before")
    @classmethod
    def _coerce_style(cls, value: Any) -> StylePreset:
        if isinstance(value, StylePreset):
            return value
        return resolve_style_preset(value)


class ImageGenerationResponse(BaseModel):
    success: bool
    images: list[GeneratedImage] = []
    provider: str | None = None
    duration_ms: float | None = None
    error: str | None = None


class AsyncImageGenerationRequest(ImageGenerationRequest):
    callback_url: str | None = None


class AsyncImageTaskSubmitResponse(BaseModel):
    task_id: str
    status: str
    message: str


class AsyncImageArtifact(BaseModel):
    artifact_id: str | None = None
    download_url: str | None = None
    url: str
    width: int | None = None
    height: int | None = None


class AsyncImageTaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: int
    prompt: str
    model_id: str
    provider: str | None = None
    images: list[AsyncImageArtifact] = []
    duration_ms: float | None = None
    error: str | None = None
    created_at: str
    completed_at: str | None = None


# -----------------------------------------------------------------------------
# In-memory task store for async generation
# -----------------------------------------------------------------------------

_image_tasks: dict[str, dict] = {}
_MAX_TASKS = 500


def _cleanup_old_tasks() -> None:
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


async def _upload_to_gemini_files(gemini, result_image: dict) -> str | None:
    """Upload a freshly generated image to Gemini Files API, return its URI."""
    try:
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


# -----------------------------------------------------------------------------
# POST /generate-image — synchronous
# -----------------------------------------------------------------------------


@router.post("/generate-image", response_model=ImageGenerationResponse)
async def generate_image(
    body: ImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> ImageGenerationResponse:
    """Synchronous image generation — routes through Gemini / Doubao / DashScope."""
    start_time = time.time()
    try:
        model_info = model_registry.get_model(body.model_id) if model_registry else None
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
            cb64 = img.get("content_base64", "")
            mt = img.get("mime_type", "image/png")
            if body.add_watermark and cb64:
                cb64, mt = await asyncio.to_thread(apply_watermark_b64, cb64)
            return f"data:{mt};base64,{cb64}"

        session_mgr = getattr(request.app.state, "session_manager", None)

        # App-driven edit mode: caller sends reference_image, stateless.
        if body.reference_image and prefer_gemini:
            gemini = get_gemini_image_generator()
            if gemini.is_configured:
                preset = body.style
                styled_prompt = compose_styled_prompt(body.prompt, preset)
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

        # Multi-turn Gemini chat — persisted history in session metadata.
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
                locked_preset = resolve_style_preset(session.metadata.get("style_preset"))

            effective_preset = body.style if body.style is not StylePreset.DEFAULT else locked_preset
            styled_prompt = compose_styled_prompt(body.prompt, effective_preset)
            contents = build_gemini_contents_from_history(image_history, styled_prompt)

            res = await gemini.generate_chat(contents=contents, n=body.n, aspect_ratio=aspect_ratio)

            if res.success and res.images:
                file_uri = await _upload_to_gemini_files(gemini, res.images[0])
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
            return ImageGenerationResponse(
                success=True,
                images=[GeneratedImage(url=u, width=width, height=height) for u in urls],
                provider="google",
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
            )

        # Single-turn fallback — smart router picks provider.
        preset = body.style
        styled_prompt = compose_styled_prompt(body.prompt, preset)
        dashscope_tag = resolve_dashscope_style_tag(preset)
        negative_prompt = resolve_negative_prompt(preset)
        router_svc = get_smart_image_generator()

        res = await router_svc.generate(
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
        return ImageGenerationResponse(
            success=True,
            images=[GeneratedImage(url=u, width=width, height=height) for u in urls],
            provider=res.provider,
            duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
        )

    except Exception as e:
        logger.exception("Image generation failed: %s", e)
        return ImageGenerationResponse(
            success=False, images=[], provider="unknown",
            duration_ms=(time.time() - start_time) * 1000, error=str(e),
        )


# -----------------------------------------------------------------------------
# POST /generate-image-async + GET /image-task/{task_id}
# -----------------------------------------------------------------------------


def _get_artifact_storage():
    """Artifact storage lives in gateway's src/services/storage; the
    assistant-service Docker image bundles it. Return None if the module
    isn't reachable (dev + tests) so the async task still returns
    base64-encoded images without persisting to S3."""
    try:
        from src.services.storage import get_artifact_storage
        return get_artifact_storage()
    except Exception:
        return None


async def _process_image_result(
    img: dict, *, task_id: str, prompt: str, width: int, height: int,
    add_watermark: bool, session_id: str | None, user: UserContext,
    index: int, artifact_storage,
) -> dict:
    content_base64 = img.get("content_base64", "")
    mime_type = img.get("mime_type", "image/png")

    if add_watermark and content_base64:
        try:
            content_base64, mime_type = await asyncio.to_thread(apply_watermark_b64, content_base64)
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
                session_id=session_id, tenant_id=user.tenant_id, user_id=user.user_id,
                type="image", format=ext, title=f"Generated: {prompt[:40]}...",
                filename=filename, content=content, source="image_generation",
            )
            entry["artifact_id"] = artifact.artifact_id
            entry["download_url"] = await artifact_storage.get_presigned_download_url(artifact)
        except Exception as e:
            logger.warning("Failed to save async image artifact: %s", e)
    return entry


async def _run_image_generation_task(
    task_id: str, body: AsyncImageGenerationRequest,
    model_registry: ModelRegistry, user: UserContext, session_manager=None,
) -> None:
    task = _image_tasks[task_id]
    task["status"] = "running"
    task["progress"] = 10
    start_time = time.time()

    try:
        model_info = model_registry.get_model(body.model_id) if model_registry else None
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id, selected_provider,
        )
        width, height, aspect_ratio = parse_image_size(body.size)
        task["progress"] = 30

        res = None
        if body.reference_image and prefer_gemini:
            gemini = get_gemini_image_generator()
            if gemini.is_configured:
                styled_prompt = compose_styled_prompt(body.prompt, body.style)
                res = await gemini.generate(
                    prompt=styled_prompt, n=body.n, aspect_ratio=aspect_ratio,
                    reference_image=body.reference_image,
                )

        if res is None and body.session_id and session_manager and prefer_gemini:
            gemini = get_gemini_image_generator()
            if gemini.is_configured:
                session = await session_manager.get(body.session_id)
                if not session:
                    session = await session_manager.create(
                        user_id=user.user_id, tenant_id=user.tenant_id,
                        session_id=body.session_id,
                        metadata={"image_chat_history": []},
                    )
                image_history: list[dict] = []
                locked_preset = StylePreset.DEFAULT
                if session and session.metadata:
                    image_history = session.metadata.get("image_chat_history", [])
                    locked_preset = resolve_style_preset(session.metadata.get("style_preset"))

                effective_preset = body.style if body.style is not StylePreset.DEFAULT else locked_preset
                styled_prompt = compose_styled_prompt(body.prompt, effective_preset)
                contents = build_gemini_contents_from_history(image_history, styled_prompt)
                res = await gemini.generate_chat(contents=contents, n=body.n, aspect_ratio=aspect_ratio)

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

        if res is None:
            styled_prompt = compose_styled_prompt(body.prompt, body.style)
            dashscope_tag = resolve_dashscope_style_tag(body.style)
            negative_prompt = resolve_negative_prompt(body.style)
            router_svc = get_smart_image_generator()
            res = await router_svc.generate(
                prompt=styled_prompt, n=body.n, size=body.size or "1024*1024",
                style=dashscope_tag, negative_prompt=negative_prompt,
                aspect_ratio=aspect_ratio,
                prefer_gemini=prefer_gemini, prefer_doubao=prefer_doubao,
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
        artifact_storage = _get_artifact_storage()
        images = await asyncio.gather(*[
            _process_image_result(
                img, task_id=task_id, prompt=body.prompt,
                width=width, height=height,
                add_watermark=body.add_watermark,
                session_id=body.session_id, user=user, index=i,
                artifact_storage=artifact_storage,
            )
            for i, img in enumerate(res.images)
        ])
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
    _cleanup_old_tasks()
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    _image_tasks[task_id] = {
        "task_id": task_id, "status": "pending", "progress": 0,
        "prompt": body.prompt, "model_id": body.model_id, "provider": None,
        "images": [], "duration_ms": None, "error": None,
        "created_at": now, "completed_at": None,
    }
    session_mgr = get_session_manager(request)
    asyncio.create_task(
        _run_image_generation_task(task_id, body, model_registry, user, session_manager=session_mgr)
    )
    return AsyncImageTaskSubmitResponse(
        task_id=task_id, status="pending",
        message="Image generation task submitted",
    )


@router.get("/image-task/{task_id}", response_model=AsyncImageTaskStatusResponse)
async def get_image_task_status(
    task_id: str,
    user: UserContext = Depends(get_user_context),
) -> AsyncImageTaskStatusResponse:
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
        task_id=task["task_id"], status=task["status"],
        progress=task.get("progress", 0),
        prompt=task["prompt"], model_id=task["model_id"],
        provider=task.get("provider"), images=images,
        duration_ms=task.get("duration_ms"), error=task.get("error"),
        created_at=task["created_at"], completed_at=task.get("completed_at"),
    )
