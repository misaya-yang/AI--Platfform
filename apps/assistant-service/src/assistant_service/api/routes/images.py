"""Image generation endpoints — sync + async + polling.

Owned by assistant-service; gateway forwards ``/api/v1/assistant/generate-image``,
``/api/v1/assistant/generate-image-async``, and
``/api/v1/assistant/image-task/{task_id}`` here via the shared ServiceProxy.

## Architecture

Public API contract — Dev-backend-facing, URL-only:

- Generated image bytes are stored in our own object storage (S3/MinIO via
  ``ArtifactStorage``). The response carries a presigned download URL plus
  ``artifact_id``. The Dev backend forwards the URL to its frontend; the
  frontend fetches the bytes directly from S3 (CDN-cacheable).
- For multi-turn editing the request body is just ``session_id + prompt``
  — no image bytes flow over the Dev → AS edge after turn 1.
- For stateless edits, the Dev backend can pass ``reference_image_url``
  (URL of the prior image we returned them) instead of base64. AS fetches
  the bytes server-side and passes them to Gemini as ``inlineData``.

We DO NOT use Gemini's Files API:
- ``Vertex Express`` (free tier) cannot reference URIs uploaded to AI Studio
  → cross-host 403 on second turn.
- 48 h URI expiry breaks "user comes back tomorrow to keep editing".
- Vendor-locks our scale to Google's storage product.

The base64 representation of the image bytes only ever lives on the
``AS ↔ Gemini`` server-to-server hop (Gemini's API requires inline data).
It never appears in the Dev-facing request or response.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.image import (
    append_image_turns,
    apply_watermark_b64,
    build_gemini_contents_from_history,
    inflate_history_with_bytes,
    parse_image_size,
    resolve_image_routing,
    send_image_callback,
)
from ai_gateway_core.security import SafeFetchError, safe_fetch
from ai_gateway_core.style_presets import (
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ...auth import UserContext, get_user_context
from ...core.models.model_registry import ModelRegistry
from ...core.tools.gemini_image_tool import get_gemini_image_generator
from ...core.tools.smart_image_generator import get_smart_image_generator
from ..deps import get_model_registry, get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class GeneratedImage(BaseModel):
    """Generated image — points to S3-backed artifact when storage is configured."""

    url: str = Field(
        ...,
        description=(
            "Presigned download URL (S3) when artifact storage is configured. "
            "Falls back to a ``data:image/...;base64,...`` URL only when "
            "ArtifactStorage is unavailable (dev/tests)."
        ),
    )
    width: int | None = None
    height: int | None = None
    artifact_id: str | None = Field(
        default=None,
        description="Stable ID for this generated image. Use as `reference_image_url` "
                    "lookup or persist for later retrieval.",
    )


class ImageGenerationRequest(BaseModel):
    """Image generation request — three editing modes:

    1. **Fresh generation**: just ``prompt`` + ``model_id``.
    2. **Stateless edit**: ``prompt`` + ``reference_image`` (base64) OR
       ``reference_image_url`` (URL we returned earlier — preferred, avoids
       Dev backend re-uploading bytes).
    3. **Stateful multi-turn**: ``prompt`` + ``session_id`` (server holds
       the editing history).
    """

    prompt: str = Field(..., min_length=1, max_length=4000)
    model_id: str = "qwen-image-2.0"
    n: int = Field(1, ge=1, le=4)
    size: str | None = "1024*1024"
    style: StylePreset = Field(default=StylePreset.DEFAULT)
    session_id: str | None = None
    reference_artifact_id: str | None = Field(
        default=None,
        description=(
            "Stable artifact ID returned by an earlier generation (preferred "
            "for stateless edits). The server looks up bytes directly via "
            "ArtifactStorage — no URL fetch, no SSRF surface. Use this when "
            "you have the artifact_id we returned previously."
        ),
    )
    reference_image: str | None = Field(
        default=None,
        description=(
            "Reference image as base64 or data URL. Use only when the prior "
            "image is local-only (e.g. a user upload that never went through "
            "us). Prefer ``reference_artifact_id`` (server-side lookup) or "
            "``reference_image_url`` (we fetch via SSRF-safe client)."
        ),
    )
    reference_image_url: str | None = Field(
        default=None,
        description=(
            "URL of a prior image. AS fetches it via SSRF-safe client (DNS "
            "pinning + private-IP rejection + 8 MB streaming cap). Only "
            "http(s); private/loopback/link-local rejected. Prefer "
            "``reference_artifact_id`` when possible — that path doesn't "
            "fetch a URL at all."
        ),
    )
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
    session_id: str | None = Field(
        default=None,
        description="Echo of session_id when stateful multi-turn was used.",
    )


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
# Async task store
# -----------------------------------------------------------------------------
#
# Two backends, picked at request time based on whether Redis is wired up:
#
# 1. **Redis** (preferred) — survives container restarts and works across
#    multiple AS replicas. Tasks expire after 1 hour via Redis ``EX`` so
#    completed-task cleanup is automatic.
# 2. **In-process dict** (fallback) — for dev/test where Redis isn't
#    available. Has a soft 500-entry cap; oldest completed/failed tasks
#    over an hour old are evicted on each new submit.
#
# The fallback path is intentionally not deleted: integration tests run
# without Redis, and a single-replica dev deployment doesn't need it.

_image_tasks: dict[str, dict] = {}
_MAX_TASKS = 500
_TASK_TTL_SECONDS = 3600
_TASK_KEY_PREFIX = "image_task:"

# Strong refs to in-flight async-generation workers. ``asyncio.create_task``
# only weak-references the task; without this set the task can be GC'd
# mid-execution under load. Each worker self-removes via ``done_callback``.
_in_flight_workers: set[asyncio.Task[None]] = set()


def _cleanup_old_tasks() -> None:
    """Evict completed/failed in-process tasks older than the TTL.

    No-op for the Redis backend (Redis ``EX`` handles expiry)."""
    if len(_image_tasks) < _MAX_TASKS:
        return
    now = datetime.now(timezone.utc)
    to_remove = []
    for tid, task in _image_tasks.items():
        if task["status"] in ("completed", "failed"):
            created = datetime.fromisoformat(task["created_at"])
            if (now - created).total_seconds() > _TASK_TTL_SECONDS:
                to_remove.append(tid)
    for tid in to_remove:
        _image_tasks.pop(tid, None)


async def _store_task(redis, task_id: str, task: dict) -> None:
    """Persist ``task`` under ``task_id``. Uses Redis when present, else the
    in-process dict. Always refreshes the TTL on Redis writes — completed
    tasks then expire 1h after completion regardless of when submitted.

    Recovery: when a Redis write succeeds after a previous fallback, we
    proactively pop the dict entry. Without this, ``_load_task`` keeps
    preferring the now-stale dict value forever (it's only ever cleared
    by the size-cap GC). After recovery, Redis is again the authoritative
    source.
    """
    if redis is not None:
        try:
            await redis.set(
                _TASK_KEY_PREFIX + task_id,
                json.dumps(task, default=str),
                ex=_TASK_TTL_SECONDS,
            )
            # Redis is now authoritative — drop any stale fallback entry.
            _image_tasks.pop(task_id, None)
            return
        except Exception as exc:
            logger.warning("Redis task store write failed (%s); falling back to dict", exc)
    _image_tasks[task_id] = task


async def _load_task(redis, task_id: str) -> dict | None:
    """Look up a task by id. Tries Redis first, falls back to dict.

    The fallback dict is preferred over a successful Redis read when both
    have an entry: the dict is only populated when a prior write failed to
    Redis, so its presence means it holds the freshest known state and
    Redis may be stale after recovery."""
    redis_task: dict | None = None
    if redis is not None:
        try:
            raw = await redis.get(_TASK_KEY_PREFIX + task_id)
            if raw:
                redis_task = json.loads(raw)
        except Exception as exc:
            logger.warning("Redis task store read failed (%s); falling back to dict", exc)
    # Fallback dict wins when present — it's only populated on Redis write
    # failure so it's authoritative over a possibly-stale Redis value.
    if task_id in _image_tasks:
        return _image_tasks[task_id]
    return redis_task


# -----------------------------------------------------------------------------
# Artifact storage helpers
# -----------------------------------------------------------------------------


def _get_artifact_storage():
    """Artifact storage lives in ai_gateway_core (since Phase 5f Batch B).
    Return None if the module isn't reachable (dev + tests) — callers fall
    back to data URLs in the response and skip session-history persistence
    (multi-turn won't work)."""
    try:
        from ai_gateway_core.storage import get_artifact_storage
        return get_artifact_storage()
    except Exception:
        return None


async def _persist_and_get_url(
    img: dict,
    *,
    artifact_storage,
    session_id: str | None,
    user: UserContext,
    prompt: str,
    add_watermark: bool,
    width: int,
    height: int,
    index: int,
) -> tuple[str | None, GeneratedImage]:
    """Persist artifact(s) and return (raw_artifact_id_for_history, public_GeneratedImage).

    When ``add_watermark`` is True we store two artifacts: a raw one for
    multi-turn history replay (so next-turn Gemini sees the unwatermarked
    image and doesn't accumulate watermarks) and a watermarked one whose
    URL we return. When ``add_watermark`` is False, the same artifact serves
    both roles.

    The first element of the returned tuple is the **raw** artifact_id (used
    by ``_persist_multi_turn_result`` to write into session history). The
    ``artifact_id`` on the returned ``GeneratedImage`` is the **watermarked**
    one — that's what the Dev backend / frontend sees in the response and
    will hand back as ``reference_image_url`` for stateless edits.
    """
    raw_b64 = img.get("content_base64", "")
    raw_mt = img.get("mime_type", "image/png")

    if not artifact_storage or not raw_b64:
        # Fallback path: data URL response, no artifacts.
        if add_watermark and raw_b64:
            try:
                cb64, mt = await asyncio.to_thread(apply_watermark_b64, raw_b64)
            except Exception as e:
                logger.warning("Watermark failed for image %d: %s", index, e)
                cb64, mt = raw_b64, raw_mt
        else:
            cb64, mt = raw_b64, raw_mt
        return None, GeneratedImage(
            url=f"data:{mt};base64,{cb64}",
            width=width, height=height, artifact_id=None,
        )

    # Storage available — persist raw artifact first (always).
    raw_artifact_id: str | None = None
    raw_url: str | None = None
    effective_session = session_id or f"stateless_{uuid.uuid4().hex[:12]}"
    try:
        raw_bytes = _b64.b64decode(raw_b64)
        ext = raw_mt.split("/")[-1] or "png"
        raw_artifact = await artifact_storage.create_artifact(
            session_id=effective_session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            type="image",
            format=ext,
            title=f"Generated: {prompt[:60]}",
            filename=f"generated_{uuid.uuid4().hex[:8]}_{index + 1}.{ext}",
            content=raw_bytes,
            source="image_generation",
        )
        raw_artifact_id = raw_artifact.artifact_id
        raw_url = await artifact_storage.get_presigned_download_url(raw_artifact)
    except Exception as e:
        logger.warning("Failed to save raw image artifact: %s", e)
        # Fall back to data URL as response, no artifact.
        cb64 = raw_b64
        mt = raw_mt
        if add_watermark:
            try:
                cb64, mt = await asyncio.to_thread(apply_watermark_b64, raw_b64)
            except Exception as we:
                logger.warning("Watermark failed for image %d: %s", index, we)
        return None, GeneratedImage(
            url=f"data:{mt};base64,{cb64}",
            width=width, height=height, artifact_id=None,
        )

    if not add_watermark:
        # Single-artifact happy path.
        return raw_artifact_id, GeneratedImage(
            url=raw_url or f"data:{raw_mt};base64,{raw_b64}",
            width=width, height=height, artifact_id=raw_artifact_id,
        )

    # add_watermark=True → store a separate watermarked artifact for the public URL.
    try:
        wm_b64, wm_mt = await asyncio.to_thread(apply_watermark_b64, raw_b64)
        wm_bytes = _b64.b64decode(wm_b64)
        wm_ext = wm_mt.split("/")[-1] or "png"
        wm_artifact = await artifact_storage.create_artifact(
            session_id=effective_session,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            type="image",
            format=wm_ext,
            title=f"Watermarked: {prompt[:60]}",
            filename=f"watermarked_{uuid.uuid4().hex[:8]}_{index + 1}.{wm_ext}",
            content=wm_bytes,
            source="image_generation_watermarked",
        )
        public_artifact_id = wm_artifact.artifact_id
        public_url = await artifact_storage.get_presigned_download_url(wm_artifact)
    except Exception as e:
        logger.warning("Watermarked artifact persist failed (%s); returning raw URL", e)
        # Degrade: return raw URL but still keep raw_artifact_id for history.
        return raw_artifact_id, GeneratedImage(
            url=raw_url or f"data:{raw_mt};base64,{raw_b64}",
            width=width, height=height, artifact_id=raw_artifact_id,
        )

    return raw_artifact_id, GeneratedImage(
        url=public_url or raw_url or f"data:{raw_mt};base64,{raw_b64}",
        width=width, height=height, artifact_id=public_artifact_id,
    )


async def _resolve_reference_bytes(
    body: ImageGenerationRequest, *, artifact_storage, user: UserContext | None = None,
) -> str | None:
    """Resolve any reference image to a base64 string suitable for Gemini.

    Order (most-secure first):
    1. ``reference_artifact_id`` — direct lookup in our ArtifactStorage. No
       URL fetch, no SSRF surface. **Preferred** for stateless edits.
    2. ``reference_image`` (base64 / data-URL) — pass through. Use only when
       caller has bytes locally and isn't going through us.
    3. ``reference_image_url`` — SSRF-safe fetch via shared ``safe_fetch``
       (DNS pinning + private/loopback rejection + 8 MB streaming cap +
       urljoin redirect handling). Base64-encoded for Gemini ``inlineData``.

    Returns ``None`` if no reference is provided.
    """
    if body.reference_artifact_id:
        if not artifact_storage:
            raise HTTPException(
                status_code=503,
                detail="reference_artifact_id requires ArtifactStorage to be configured",
            )
        # Ownership check — without this, any caller could pass any artifact_id
        # they observed from logs / network and read another user's image.
        # Fetch metadata first (cheap), check tenant+user, then download.
        try:
            artifact = await artifact_storage.get_artifact(body.reference_artifact_id)
        except Exception as exc:
            logger.warning("reference_artifact_id lookup failed: %s", exc)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to load reference_artifact_id: {exc}",
            ) from exc
        # 404 (not 403) for both not-found and wrong-owner — same error code
        # so an attacker can't use timing/code distinction to enumerate IDs.
        if (
            artifact is None
            or (user is not None and artifact.user_id != user.user_id)
            or (user is not None and artifact.tenant_id != user.tenant_id)
        ):
            raise HTTPException(
                status_code=404,
                detail=f"reference_artifact_id {body.reference_artifact_id!r} not found",
            )
        try:
            content = await artifact_storage.download_artifact(body.reference_artifact_id)
        except Exception as exc:
            logger.warning("reference_artifact_id download failed: %s", exc)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to load reference_artifact_id: {exc}",
            ) from exc
        if not content:
            raise HTTPException(
                status_code=404,
                detail=f"reference_artifact_id {body.reference_artifact_id!r} not found",
            )
        return _b64.b64encode(content).decode()

    if body.reference_image:
        return body.reference_image

    if not body.reference_image_url:
        return None

    try:
        content = await safe_fetch(
            body.reference_image_url,
            max_bytes=8 * 1024 * 1024,
            max_redirects=3,
            timeout=30.0,
        )
        return _b64.b64encode(content).decode()
    except SafeFetchError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=f"reference_image_url: {exc}",
        ) from exc
    except Exception as exc:
        logger.warning("reference_image_url fetch failed: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch reference_image_url: {exc}",
        ) from exc


async def _download_artifact_bytes(artifact_storage, artifact_id: str) -> bytes | None:
    """Fetch raw bytes for an artifact, swallowing storage errors so
    ``inflate_history_with_bytes`` can degrade a single missing turn instead
    of aborting the whole replay."""
    if not artifact_storage:
        return None
    try:
        return await artifact_storage.download_artifact(artifact_id)
    except Exception as exc:
        logger.warning("Artifact download failed for %s: %s", artifact_id, exc)
        return None


# -----------------------------------------------------------------------------
# Multi-turn editing — Gemini chat with S3-backed history
# -----------------------------------------------------------------------------


async def _run_gemini_multi_turn(
    body: ImageGenerationRequest,
    *,
    aspect_ratio: str,
    width: int,
    height: int,
    session_manager,
    user: UserContext,
    artifact_storage,
):
    """Run one turn of a stateful multi-turn editing session.

    Returns the raw Gemini result + the resolved style preset; the caller
    is responsible for persisting the new turn back to the session and for
    building the public response shape.
    """
    if not body.session_id:
        raise ValueError("session_id required for multi-turn flow")

    gemini = get_gemini_image_generator()
    if not gemini.is_configured:
        return None, None, "Gemini API key not configured for multi-turn image chat"

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
        locked_preset = resolve_style_preset(session.metadata.get("style_preset"))

    effective_preset = (
        body.style if body.style is not StylePreset.DEFAULT else locked_preset
    )
    styled_prompt = compose_styled_prompt(body.prompt, effective_preset)

    # Inflate history pointers → real bytes for Gemini's inlineData.
    async def _download(aid: str) -> bytes | None:
        return await _download_artifact_bytes(artifact_storage, aid)

    resolved_history = await inflate_history_with_bytes(image_history, _download)
    contents = build_gemini_contents_from_history(resolved_history, styled_prompt)

    res = await gemini.generate_chat(
        contents=contents, n=body.n, aspect_ratio=aspect_ratio,
    )
    return res, (session, image_history, effective_preset), None


async def _persist_multi_turn_result(
    body: ImageGenerationRequest,
    *,
    res,
    session_state: tuple,
    session_manager,
    user: UserContext,
    artifact_storage,
    width: int,
    height: int,
) -> tuple[str | None, list[GeneratedImage]]:
    """Store ALL newly-generated images as artifacts (concurrently), append a
    single canonical turn to history, persist the session metadata. Returns
    ``(canonical_artifact_id, [GeneratedImage, ...])`` — the canonical id is
    the first image's id, used as the visual anchor for the next-turn replay.

    History only records the canonical (first) image: putting all n>1 images
    into next turn's context would balloon the prompt and make replay slow
    while adding little signal beyond the visual anchor.
    """
    session, image_history, effective_preset = session_state

    persisted = await asyncio.gather(*[
        _persist_and_get_url(
            img,
            artifact_storage=artifact_storage,
            session_id=body.session_id,
            user=user,
            prompt=body.prompt,
            add_watermark=body.add_watermark,
            width=width,
            height=height,
            index=i,
        )
        for i, img in enumerate(res.images)
    ])
    canonical_artifact_id = persisted[0][0]
    canonical_img_payload = res.images[0]
    generated_list = [gi for _, gi in persisted]

    # History gets only the first image — multi-image n>1 still has just one
    # canonical visual anchor for next-turn replay.
    append_image_turns(
        image_history, body.prompt, canonical_img_payload, res.text,
        artifact_id=canonical_artifact_id,
    )

    if session_manager and session:
        meta = dict(session.metadata or {})
        meta["image_chat_history"] = image_history
        meta["style_preset"] = effective_preset.value
        try:
            await session_manager.update_metadata(body.session_id, meta)
        except Exception as e:
            logger.warning(
                "Session metadata write failed for %s: %s — continuing with "
                "in-flight history (next turn will not see this image)",
                body.session_id, e,
            )

    return canonical_artifact_id, generated_list


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
    """Synchronous image generation — Gemini multi-turn / stateless edit / fresh."""
    start_time = time.time()
    try:
        model_info = model_registry.get_model(body.model_id) if model_registry else None
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id, selected_provider,
        )

        has_reference = bool(
            body.reference_artifact_id
            or body.reference_image
            or body.reference_image_url
        )
        if has_reference and not prefer_gemini:
            # Editing an existing image only works on Gemini. Falling through
            # to fresh generation would silently ignore the user's reference
            # and produce a totally unrelated image — almost always wrong.
            return ImageGenerationResponse(
                success=False, images=[], provider=str(selected_provider or "unknown"),
                duration_ms=(time.time() - start_time) * 1000,
                error=(
                    f"reference image editing requires a Gemini model "
                    f"(got model_id={body.model_id!r}, provider={selected_provider!r}). "
                    "Drop the reference fields for fresh generation, or pick a "
                    "Gemini model_id (e.g. gemini-3-flash-preview)."
                ),
            )

        width, height, aspect_ratio = parse_image_size(body.size)

        artifact_storage = _get_artifact_storage()
        session_mgr = getattr(request.app.state, "session_manager", None)

        # ---- Stateless edit (caller passes prior image) ------------------
        if has_reference and prefer_gemini:
            ref_b64 = await _resolve_reference_bytes(
                body, artifact_storage=artifact_storage, user=user,
            )
            gemini = get_gemini_image_generator()
            if not gemini.is_configured:
                return ImageGenerationResponse(
                    success=False, images=[], provider="none",
                    duration_ms=(time.time() - start_time) * 1000,
                    error="Gemini API key not configured",
                )

            styled_prompt = compose_styled_prompt(body.prompt, body.style)
            res = await gemini.generate(
                prompt=styled_prompt,
                n=body.n,
                aspect_ratio=aspect_ratio,
                reference_image=ref_b64,
            )
            if not res.success or not res.images:
                return ImageGenerationResponse(
                    success=False, images=[], provider="google",
                    duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                    error=res.error or "Image edit failed",
                )

            persisted = await asyncio.gather(*[
                _persist_and_get_url(
                    img, artifact_storage=artifact_storage,
                    session_id=body.session_id, user=user, prompt=body.prompt,
                    add_watermark=body.add_watermark, width=width, height=height,
                    index=i,
                )
                for i, img in enumerate(res.images)
            ])
            return ImageGenerationResponse(
                success=True,
                images=[gi for _, gi in persisted],
                provider="google",
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
            )

        # ---- Stateful multi-turn (session-backed) -----------------------
        if body.session_id and session_mgr and prefer_gemini:
            res, session_state, err = await _run_gemini_multi_turn(
                body, aspect_ratio=aspect_ratio, width=width, height=height,
                session_manager=session_mgr, user=user,
                artifact_storage=artifact_storage,
            )
            if err:
                return ImageGenerationResponse(
                    success=False, images=[], provider="none",
                    duration_ms=(time.time() - start_time) * 1000, error=err,
                    session_id=body.session_id,
                )
            if not res.success or not res.images:
                return ImageGenerationResponse(
                    success=False, images=[], provider="google",
                    duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                    error=res.error or "Image generation failed",
                    session_id=body.session_id,
                )

            _, generated_list = await _persist_multi_turn_result(
                body, res=res, session_state=session_state,
                session_manager=session_mgr, user=user,
                artifact_storage=artifact_storage,
                width=width, height=height,
            )
            return ImageGenerationResponse(
                success=True, images=generated_list,
                provider="google",
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                session_id=body.session_id,
            )

        # ---- Fresh single-turn — smart router picks provider ------------
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

        if not res.success:
            err = res.error or "Image generation failed"
            if res.blocked and res.block_reason:
                err = f"{err} (blocked: {res.block_reason})"
            return ImageGenerationResponse(
                success=False, images=[], provider=res.provider,
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                error=err,
            )

        persisted = await asyncio.gather(*[
            _persist_and_get_url(
                img, artifact_storage=artifact_storage,
                session_id=body.session_id, user=user, prompt=body.prompt,
                add_watermark=body.add_watermark, width=width, height=height,
                index=i,
            )
            for i, img in enumerate(res.images)
        ])
        return ImageGenerationResponse(
            success=True,
            images=[gi for _, gi in persisted],
            provider=res.provider,
            duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Image generation failed: %s", e)
        return ImageGenerationResponse(
            success=False, images=[], provider="unknown",
            duration_ms=(time.time() - start_time) * 1000, error=str(e),
        )


# -----------------------------------------------------------------------------
# POST /generate-image-async + GET /image-task/{task_id}
# -----------------------------------------------------------------------------


async def _run_image_generation_task(
    task_id: str, body: AsyncImageGenerationRequest,
    model_registry: ModelRegistry, user: UserContext,
    session_manager=None, redis=None,
) -> None:
    task = await _load_task(redis, task_id)
    if task is None:
        # Defensive: caller should have stored the task before scheduling us.
        # If we get here the submit path raced or the storage backend dropped
        # it; bail rather than crash the worker.
        logger.error("Async image task %s vanished before worker started", task_id)
        return
    task["status"] = "running"
    task["progress"] = 10
    await _store_task(redis, task_id, task)
    start_time = time.time()

    try:
        model_info = model_registry.get_model(body.model_id) if model_registry else None
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id, selected_provider,
        )
        width, height, aspect_ratio = parse_image_size(body.size)
        task["progress"] = 30

        artifact_storage = _get_artifact_storage()
        has_reference = bool(
            body.reference_artifact_id
            or body.reference_image
            or body.reference_image_url
        )

        # Reference given but routing isn't Gemini — fail explicitly. Falling
        # through to fresh generation would silently produce an unrelated
        # image, which is almost never what the caller wanted.
        if has_reference and not prefer_gemini:
            task["status"] = "failed"
            task["error"] = (
                f"reference image editing requires a Gemini model "
                f"(got model_id={body.model_id!r}, provider={selected_provider!r})"
            )
            task["progress"] = 100
            task["completed_at"] = datetime.now(timezone.utc).isoformat()
            await _store_task(redis, task_id, task)
            return

        res = None
        generated_response_imgs: list[GeneratedImage] = []

        if has_reference and prefer_gemini:
            gemini = get_gemini_image_generator()
            if not gemini.is_configured:
                task["status"] = "failed"
                task["error"] = "Gemini API key not configured"
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await _store_task(redis, task_id, task)
                return
            ref_b64 = await _resolve_reference_bytes(
                body, artifact_storage=artifact_storage, user=user,
            )
            styled_prompt = compose_styled_prompt(body.prompt, body.style)
            res = await gemini.generate(
                prompt=styled_prompt, n=body.n, aspect_ratio=aspect_ratio,
                reference_image=ref_b64,
            )

        if res is None and body.session_id and session_manager and prefer_gemini:
            res, session_state, err = await _run_gemini_multi_turn(
                body, aspect_ratio=aspect_ratio, width=width, height=height,
                session_manager=session_manager, user=user,
                artifact_storage=artifact_storage,
            )
            if err:
                task["status"] = "failed"
                task["error"] = err
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await _store_task(redis, task_id, task)
                return

            if res and res.success and res.images:
                _, generated_list = await _persist_multi_turn_result(
                    body, res=res, session_state=session_state,
                    session_manager=session_manager, user=user,
                    artifact_storage=artifact_storage,
                    width=width, height=height,
                )
                generated_response_imgs.extend(generated_list)

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
            await _store_task(redis, task_id, task)
            return

        task["progress"] = 70

        # If the multi-turn helper already persisted, reuse those entries.
        if not generated_response_imgs:
            persisted = await asyncio.gather(*[
                _persist_and_get_url(
                    img, artifact_storage=artifact_storage,
                    session_id=body.session_id, user=user, prompt=body.prompt,
                    add_watermark=body.add_watermark, width=width, height=height,
                    index=i,
                )
                for i, img in enumerate(res.images)
            ])
            generated_response_imgs = [gi for _, gi in persisted]

        task["images"] = [
            {
                "url": gi.url,
                "width": gi.width,
                "height": gi.height,
                "artifact_id": gi.artifact_id,
                "download_url": gi.url if gi.artifact_id else None,
            }
            for gi in generated_response_imgs
        ]
        task["status"] = "completed"
        task["progress"] = 100
        task["completed_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        # logger.exception captures the stack trace; previous logger.error
        # only logged str(e), making prod debugging impossible.
        logger.exception("Async image generation task %s failed", task_id)
        task["status"] = "failed"
        task["error"] = str(e)
        task["progress"] = 100
        task["duration_ms"] = (time.time() - start_time) * 1000
        task["completed_at"] = datetime.now(timezone.utc).isoformat()

    await _store_task(redis, task_id, task)

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
    task = {
        "task_id": task_id, "status": "pending", "progress": 0,
        "prompt": body.prompt, "model_id": body.model_id, "provider": None,
        "images": [], "duration_ms": None, "error": None,
        "created_at": now, "completed_at": None,
        # Owner — checked on poll to prevent IDOR. Without this,
        # any authenticated user could poll any UUID and read another
        # tenant's prompt + presigned image URLs.
        "owner_user_id": user.user_id,
        "owner_tenant_id": user.tenant_id,
    }
    redis = getattr(request.app.state, "redis", None)
    await _store_task(redis, task_id, task)
    session_mgr = get_session_manager(request)
    worker = asyncio.create_task(
        _run_image_generation_task(
            task_id, body, model_registry, user,
            session_manager=session_mgr, redis=redis,
        )
    )
    # Strong-ref the task so the event loop's weak-ref doesn't let it
    # be GC'd mid-execution. Self-removes on completion.
    _in_flight_workers.add(worker)
    worker.add_done_callback(_in_flight_workers.discard)
    return AsyncImageTaskSubmitResponse(
        task_id=task_id, status="pending",
        message="Image generation task submitted",
    )


@router.get("/image-task/{task_id}", response_model=AsyncImageTaskStatusResponse)
async def get_image_task_status(
    task_id: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AsyncImageTaskStatusResponse:
    redis = getattr(request.app.state, "redis", None)
    task = await _load_task(redis, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    # Ownership check — return 404 (not 403) to avoid leaking task existence
    # via timing/error-code distinction.
    owner_user_id = task.get("owner_user_id")
    owner_tenant_id = task.get("owner_tenant_id")
    if owner_user_id is not None and owner_user_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if owner_tenant_id is not None and owner_tenant_id != user.tenant_id:
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
