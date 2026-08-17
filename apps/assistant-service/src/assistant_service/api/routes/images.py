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
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.image import (
    advance_latest_artifact_cas,
    apply_watermark_b64,
    count_active_image_tasks,
    create_image_blob,
    create_image_task,
    get_image_blob,
    get_image_session,
    get_image_task,
    get_turn_by_task,
    insert_turn,
    list_turns,
    lookup_idempotent,
    new_turn_id,
    parse_image_size,
    record_idempotent,
    resolve_image_routing,
    send_image_callback,
    update_image_blob_status,
    update_turn_status,
    upsert_image_session,
)
from ai_gateway_core.image import (
    compute_owner_scope as _compute_owner_scope,
)
from ai_gateway_core.image import (
    compute_request_hash as _compute_request_hash,
)
from ai_gateway_core.image import (
    set_locked_style as _set_locked_style,
)
from ai_gateway_core.logging import record_internal_exception
from ai_gateway_core.security import SafeFetchError, safe_fetch
from ai_gateway_core.style_presets import (
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...auth import UserContext, get_user_context
from ...core.models.model_registry import ModelRegistry
from ...core.tools.gemini_image_tool import get_gemini_image_generator
from ...core.tools.smart_image_generator import get_smart_image_generator
from ..deps import get_model_registry, get_session_manager
from .image_contracts import (
    ArtifactDownloadUrlResponse as ArtifactDownloadUrlResponse,
)
from .image_contracts import (
    AsyncImageArtifact as AsyncImageArtifact,
)
from .image_contracts import (
    AsyncImageGenerationRequest as AsyncImageGenerationRequest,
)
from .image_contracts import (
    AsyncImageTaskStatusResponse as AsyncImageTaskStatusResponse,
)
from .image_contracts import (
    AsyncImageTaskSubmitResponse as AsyncImageTaskSubmitResponse,
)
from .image_contracts import (
    GeneratedImage as GeneratedImage,
)
from .image_contracts import (
    ImageBlobCompleteRequest as ImageBlobCompleteRequest,
)
from .image_contracts import (
    ImageBlobFetchUrlRequest as ImageBlobFetchUrlRequest,
)
from .image_contracts import (
    ImageBlobResponse as ImageBlobResponse,
)
from .image_contracts import (
    ImageBlobUploadUrlRequest as ImageBlobUploadUrlRequest,
)
from .image_contracts import (
    ImageBlobUploadUrlResponse as ImageBlobUploadUrlResponse,
)
from .image_contracts import (
    ImageGenerationRequest as ImageGenerationRequest,
)
from .image_contracts import (
    ImageGenerationResponse as ImageGenerationResponse,
)
from .image_contracts import (
    ImageSessionResponse as ImageSessionResponse,
)
from .image_contracts import (
    ImageTurnPublic as ImageTurnPublic,
)
from .image_generation_worker import ImageWorkerBindings, run_image_generation_task
from .image_route_helpers import (
    _blob_storage_key as _blob_storage_key,
)
from .image_route_helpers import (
    _build_variants_response as _build_variants_response,
)
from .image_route_helpers import (
    _check_expected_parent_impl,
    _check_idempotency_impl,
    _ensure_image_session_impl,
    _persist_and_get_url_bounded_impl,
    _persist_multi_turn_result_impl,
    _record_turn_impl,
    _resolve_reference_bytes_impl,
    _resolve_style_for_session_impl,
    _run_gemini_multi_turn_impl,
)
from .image_route_helpers import (
    _download_artifact_bytes as _download_artifact_bytes,
)
from .image_route_helpers import (
    _download_blob_bytes as _download_blob_bytes,
)
from .image_route_helpers import (
    _get_artifact_storage as _get_artifact_storage,
)
from .image_route_helpers import (
    _get_db_pool as _get_db_pool,
)
from .image_route_helpers import (
    _get_storage_backend as _get_storage_backend,
)
from .image_route_helpers import (
    _limit_replay_history as _limit_replay_history,
)
from .image_route_helpers import (
    _load_artifact_bytes_owner_scoped as _load_artifact_bytes_owner_scoped,
)
from .image_route_helpers import (
    _normalize_reference_image_b64 as _normalize_reference_image_b64,
)
from .image_route_helpers import (
    _request_payload_for_hash as _request_payload_for_hash,
)
from .image_route_helpers import (
    _resolve_owner_scope as _resolve_owner_scope,
)
from .image_route_helpers import (
    _resolve_owner_scope_from_user as _resolve_owner_scope_from_user,
)
from .image_route_helpers import (
    _sha256_hex as _sha256_hex,
)
from .image_route_helpers import (
    _store_blob_bytes as _store_blob_bytes,
)
from .image_route_helpers import (
    _task_is_visible_to_user as _task_is_visible_to_user,
)
from .image_route_helpers import (
    _validate_image_mime as _validate_image_mime,
)
from .image_route_helpers import (
    _validate_single_explicit_reference as _validate_single_explicit_reference,
)
from .image_task_store import (
    _MAX_TASKS as _MAX_TASKS,
)
from .image_task_store import (
    _TASK_KEY_PREFIX as _TASK_KEY_PREFIX,
)
from .image_task_store import (
    _TASK_TTL_SECONDS as _TASK_TTL_SECONDS,
)
from .image_task_store import (
    _cleanup_old_tasks as _cleanup_old_tasks,
)
from .image_task_store import (
    _image_tasks as _image_tasks,
)
from .image_task_store import (
    _in_flight_workers as _in_flight_workers,
)
from .image_task_store import (
    _load_task as _load_task,
)
from .image_task_store import (
    _persist_task_state as _persist_task_state,
)
from .image_task_store import (
    _store_task as _store_task,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_REFERENCE_MAX_BYTES = int(os.getenv("IMAGE_REFERENCE_MAX_BYTES", str(8 * 1024 * 1024)))
_REPLAY_MAX_VISUAL_TURNS = int(os.getenv("IMAGE_REPLAY_MAX_VISUAL_TURNS", "4"))
_SYNC_WAIT_SECONDS = float(os.getenv("IMAGE_SYNC_WAIT_SECONDS", "12"))
_PROVIDER_CONCURRENCY = max(1, int(os.getenv("IMAGE_PROVIDER_CONCURRENCY", "4")))
_PERSIST_CONCURRENCY = max(1, int(os.getenv("IMAGE_PERSIST_CONCURRENCY", "4")))
_SEMAPHORE_WAIT_SECONDS = float(os.getenv("IMAGE_SEMAPHORE_WAIT_SECONDS", "0.25"))
_MAX_QUEUE_DEPTH = int(os.getenv("IMAGE_MAX_QUEUE_DEPTH", "200"))
_MAX_OWNER_ACTIVE_TASKS = int(os.getenv("IMAGE_MAX_OWNER_ACTIVE_TASKS", "8"))

_provider_semaphore = asyncio.Semaphore(_PROVIDER_CONCURRENCY)
_persistence_semaphore = asyncio.Semaphore(_PERSIST_CONCURRENCY)


def _is_prod_env() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or os.getenv("ENV") or "").lower()
    return env in {"prod", "production"}


def _requires_persistent_task_store() -> bool:
    raw = os.getenv("IMAGE_REQUIRE_PERSISTENT_TASKS")
    if raw is not None:
        return raw.lower() not in {"0", "false", "no", "off"}
    return _is_prod_env()


def _sync_uses_task_queue() -> bool:
    raw = os.getenv("IMAGE_SYNC_USES_TASK_QUEUE", "1")
    return raw.lower() not in {"0", "false", "no", "off"}


@asynccontextmanager
async def _bounded(
    sem: asyncio.Semaphore,
    *,
    status_code: int,
    error_code: str,
    message: str,
    retry_after: int = 2,
):
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_SEMAPHORE_WAIT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status_code,
            detail={
                "error_code": error_code,
                "message": message,
                "retry_after": retry_after,
            },
        ) from exc
    try:
        yield
    finally:
        sem.release()


def _cap_result_images(result: Any, requested_n: int | None) -> None:
    """Keep provider over-return from changing the public n/artifact contract."""
    images = getattr(result, "images", None)
    if not isinstance(images, list):
        return
    limit = max(1, int(requested_n or 1))
    if len(images) > limit:
        logger.warning(
            "Image provider returned %d images for n=%d; capping response",
            len(images),
            limit,
        )
        result.images = images[:limit]


async def _persist_and_get_url_bounded(*args, **kwargs) -> tuple[str | None, GeneratedImage]:
    return await _persist_and_get_url_bounded_impl(
        *args,
        bounded=_bounded,
        semaphore=_persistence_semaphore,
        watermark_fn=apply_watermark_b64,
        **kwargs,
    )


async def _resolve_reference_bytes(
    body: ImageGenerationRequest,
    *,
    artifact_storage,
    user: UserContext | None = None,
    owner_scope: str | None = None,
    db_pool=None,
) -> tuple[str | None, str | None]:
    return await _resolve_reference_bytes_impl(
        **locals(),
        safe_fetch_fn=safe_fetch,
        persistent_task_store_required=_requires_persistent_task_store,
        get_image_session_fn=get_image_session,
    )


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
    return await _run_gemini_multi_turn_impl(
        **locals(),
        gemini_factory=get_gemini_image_generator,
        bounded=_bounded,
        provider_semaphore=_provider_semaphore,
    )


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
    owner_scope: str | None = None,
    turn_id: str | None = None,
    provider: str | None = "google",
    model_id: str | None = None,
    return_variants: list[str] | None = None,
    write_legacy_metadata: bool = False,
) -> tuple[str | None, list[GeneratedImage]]:
    return await _persist_multi_turn_result_impl(
        **locals(),
        cap_result_images=_cap_result_images,
        persist_image=_persist_and_get_url_bounded,
    )


async def _check_idempotency(
    pool,
    *,
    owner_scope: str,
    body: ImageGenerationRequest,
) -> tuple[str | None, bool, str | None]:
    return await _check_idempotency_impl(
        **locals(),
        compute_request_hash=_compute_request_hash,
        lookup_idempotent_fn=lookup_idempotent,
    )


async def _ensure_image_session(
    pool,
    *,
    session_id: str,
    owner_scope: str,
    user: UserContext,
    body: ImageGenerationRequest,
) -> dict | None:
    return await _ensure_image_session_impl(
        **locals(),
        get_image_session_fn=get_image_session,
        upsert_image_session_fn=upsert_image_session,
    )


async def _check_expected_parent(
    pool,
    *,
    session_id: str,
    expected_parent: str | None,
) -> None:
    await _check_expected_parent_impl(**locals(), get_image_session_fn=get_image_session)


async def _resolve_style_for_session(
    pool,
    *,
    session_id: str | None,
    body_style: StylePreset,
    style_explicit: bool,
) -> tuple[StylePreset, str | None, bool]:
    return await _resolve_style_for_session_impl(
        **locals(),
        get_image_session_fn=get_image_session,
    )


async def _record_turn(
    pool,
    *,
    turn_id: str,
    session_id: str | None,
    owner_scope: str,
    task_id: str | None,
    body: ImageGenerationRequest,
    parent_artifact_id: str | None,
    output_artifact_id: str | None,
    status: str,
    error: str | None,
    error_code: str | None,
    request_hash: str | None,
    thought_signature: str | None = None,
    provider_text: str | None = None,
    output_artifact_ids: list[str] | None = None,
    state: str | None = None,
) -> None:
    await _record_turn_impl(**locals(), insert_turn_fn=insert_turn)


# -----------------------------------------------------------------------------
# Image blob data-plane API
# -----------------------------------------------------------------------------


@router.post(
    "/image-blobs/upload-url",
    response_model=ImageBlobUploadUrlResponse,
)
async def create_image_blob_upload_url(
    body: ImageBlobUploadUrlRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageBlobUploadUrlResponse:
    pool = _get_db_pool(request)
    if pool is None and _requires_persistent_task_store():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "db_unavailable",
                "message": "Image blob database is unavailable",
            },
        )
    artifact_storage = _get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "storage_unavailable",
                "message": "ArtifactStorage not configured",
            },
        )
    mime_type = _validate_image_mime(body.mime_type)
    if body.byte_size is not None and body.byte_size > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "reference_too_large",
                "message": "image blob exceeds size limit",
            },
        )
    owner_scope = _compute_owner_scope(
        user.user_id,
        app_tenant_id=user.app_tenant_id,
        app_user_id=user.app_user_id,
    )
    blob_id = f"iblob_{uuid.uuid4().hex[:24]}"
    storage_key = _blob_storage_key(owner_scope, blob_id, body.filename)
    backend = _get_storage_backend(artifact_storage)
    upload = await backend.generate_presigned_upload_url(
        key=storage_key,
        content_type=mime_type,
        expiry_seconds=900,
        metadata={
            "blob-id": blob_id,
            "owner-scope-sha256": hashlib.sha256(owner_scope.encode("utf-8")).hexdigest(),
        },
    )
    if not upload or not upload.get("url"):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "presigned_upload_unavailable",
                "message": "Storage backend does not support presigned uploads",
            },
        )
    await create_image_blob(
        pool,
        blob_id=blob_id,
        owner_scope=owner_scope,
        content_sha256=body.content_sha256,
        byte_size=body.byte_size,
        mime_type=mime_type,
        storage_key=storage_key,
        source="presigned_upload",
        status="pending_upload",
    )
    return ImageBlobUploadUrlResponse(
        blob_id=blob_id,
        upload_url=upload["url"],
        method=upload.get("method", "PUT"),
        headers=upload.get("headers") or {"Content-Type": mime_type},
        fields=upload.get("fields"),
        storage_key=storage_key,
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat(),
    )


@router.post(
    "/image-blobs/complete",
    response_model=ImageBlobResponse,
)
async def complete_image_blob_upload(
    body: ImageBlobCompleteRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageBlobResponse:
    pool = _get_db_pool(request)
    if pool is None and _requires_persistent_task_store():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "db_unavailable",
                "message": "Image blob database is unavailable",
            },
        )
    owner_scope = _compute_owner_scope(
        user.user_id,
        app_tenant_id=user.app_tenant_id,
        app_user_id=user.app_user_id,
    )
    row = await get_image_blob(pool, blob_id=body.blob_id, owner_scope=owner_scope)
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found", "message": f"blob {body.blob_id!r} not found"},
        )
    if body.byte_size is not None and body.byte_size > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "reference_too_large",
                "message": "image blob exceeds size limit",
            },
        )
    artifact_storage = _get_artifact_storage()
    backend = _get_storage_backend(artifact_storage)
    exists = await backend.exists(row["storage_key"])
    if not exists:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "upload_missing",
                "message": "uploaded object was not found in storage",
            },
        )
    mime_type = _validate_image_mime(body.mime_type or row["mime_type"])
    await update_image_blob_status(
        pool,
        blob_id=body.blob_id,
        owner_scope=owner_scope,
        status="ready",
        content_sha256=body.content_sha256 or row.get("content_sha256"),
        byte_size=body.byte_size or row.get("byte_size"),
        mime_type=mime_type,
    )
    return ImageBlobResponse(
        blob_id=body.blob_id,
        status="ready",
        content_sha256=body.content_sha256 or row.get("content_sha256"),
        byte_size=body.byte_size or row.get("byte_size"),
        mime_type=mime_type,
        storage_key=row["storage_key"],
    )


@router.post(
    "/image-blobs/fetch-url",
    response_model=ImageBlobResponse,
)
async def fetch_image_blob_from_url(
    body: ImageBlobFetchUrlRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> ImageBlobResponse:
    pool = _get_db_pool(request)
    if pool is None and _requires_persistent_task_store():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "db_unavailable",
                "message": "Image blob database is unavailable",
            },
        )
    artifact_storage = _get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "storage_unavailable",
                "message": "ArtifactStorage not configured",
            },
        )
    try:
        content = await safe_fetch(
            body.url,
            max_bytes=_REFERENCE_MAX_BYTES,
            max_redirects=3,
            timeout=30.0,
        )
    except SafeFetchError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": "safe_fetch_failed", "message": str(exc)},
        ) from exc
    mime_type = _validate_image_mime(body.mime_type or "image/png")
    owner_scope = _compute_owner_scope(
        user.user_id,
        app_tenant_id=user.app_tenant_id,
        app_user_id=user.app_user_id,
    )
    blob = await _store_blob_bytes(
        pool=pool,
        artifact_storage=artifact_storage,
        owner_scope=owner_scope,
        user=user,
        content=content,
        mime_type=mime_type,
        source="fetch_url",
        filename="reference.png",
    )
    return ImageBlobResponse(
        blob_id=blob["blob_id"],
        status=blob["status"],
        content_sha256=blob["content_sha256"],
        byte_size=blob["byte_size"],
        mime_type=blob["mime_type"],
        storage_key=blob["storage_key"],
    )


# -----------------------------------------------------------------------------
# POST /generate-image — synchronous
# -----------------------------------------------------------------------------


def _task_to_sync_response(
    task: dict, *, idempotent_replay: bool = False
) -> ImageGenerationResponse:
    status = task.get("status", "pending")
    images = [
        GeneratedImage(
            url=img["url"],
            width=img.get("width"),
            height=img.get("height"),
            artifact_id=img.get("artifact_id"),
        )
        for img in task.get("images", [])
        if img.get("url")
    ]
    if status == "failed":
        http_status = task.get("http_status_code")
        if isinstance(http_status, int) and http_status >= 400:
            raise HTTPException(
                status_code=http_status,
                detail={
                    "error_code": task.get("error_code") or "request_failed",
                    "message": task.get("error") or "Image generation failed",
                    "task_id": task.get("task_id"),
                },
            )
        return ImageGenerationResponse(
            success=False,
            images=[],
            task_id=task.get("task_id"),
            status=status,
            provider=task.get("provider"),
            duration_ms=task.get("duration_ms"),
            error=task.get("error"),
            error_code=task.get("error_code"),
            session_id=task.get("session_id"),
            turn_id=task.get("turn_id"),
            parent_artifact_id=task.get("parent_artifact_id"),
            output_artifact_id=task.get("output_artifact_id"),
            client_request_id=task.get("client_request_id"),
            idempotent_replay=idempotent_replay,
            latest_advanced=task.get("latest_advanced", True),
        )
    return ImageGenerationResponse(
        success=status == "completed",
        images=images,
        task_id=task.get("task_id"),
        status=status,
        provider=task.get("provider"),
        duration_ms=task.get("duration_ms"),
        session_id=task.get("session_id"),
        turn_id=task.get("turn_id"),
        parent_artifact_id=task.get("parent_artifact_id"),
        output_artifact_id=task.get("output_artifact_id"),
        client_request_id=task.get("client_request_id"),
        idempotent_replay=idempotent_replay,
        latest_advanced=task.get("latest_advanced", True),
    )


def _task_from_db_row(row: dict | None) -> dict | None:
    if not row:
        return None
    result = row.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.images.internal_failure", exc
            )
            result = None
    if isinstance(result, dict):
        return result
    created_at = row.get("created_at")
    completed_at = row.get("completed_at")
    return {
        "task_id": row.get("task_id"),
        "status": row.get("status", "pending"),
        "progress": row.get("progress", 0),
        "prompt": row.get("prompt") or "",
        "model_id": row.get("model_id") or "",
        "provider": row.get("provider"),
        "images": [],
        "duration_ms": None,
        "error": row.get("error"),
        "error_code": row.get("error_code"),
        "created_at": created_at.isoformat() if created_at else "",
        "completed_at": completed_at.isoformat() if completed_at else None,
        "turn_id": row.get("turn_id"),
        "session_id": row.get("session_id"),
        "parent_artifact_id": row.get("parent_artifact_id"),
        "output_artifact_id": row.get("output_artifact_id"),
        "client_request_id": row.get("client_request_id"),
    }


async def _load_task_any(redis, pool, task_id: str) -> dict | None:
    task = await _load_task(redis, task_id)
    if task:
        return task
    return _task_from_db_row(await get_image_task(pool, task_id))


async def _generate_image_via_task(
    *,
    body: ImageGenerationRequest,
    request: Request,
    user: UserContext,
    model_registry: ModelRegistry,
) -> ImageGenerationResponse | JSONResponse:
    async_body = AsyncImageGenerationRequest(**body.model_dump(exclude_unset=True))
    submit = await submit_image_generation(
        body=async_body,
        request=request,
        user=user,
        model_registry=model_registry,
    )
    redis = getattr(request.app.state, "redis", None)
    pool = _get_db_pool(request)
    deadline = time.monotonic() + _SYNC_WAIT_SECONDS
    task: dict | None = None
    while time.monotonic() < deadline:
        task = await _load_task_any(redis, pool, submit.task_id)
        if task and task.get("status") in {"completed", "failed"}:
            return _task_to_sync_response(
                task,
                idempotent_replay="replay" in submit.message.lower(),
            )
        await asyncio.sleep(0.05)

    task = task or await _load_task_any(redis, pool, submit.task_id)
    payload = {
        "success": False,
        "task_id": submit.task_id,
        "status": (task or {}).get("status", submit.status),
        "error_code": "task_pending",
        "error": "Image generation is still running; poll /image-task/{task_id}",
        "client_request_id": body.client_request_id,
        "session_id": body.session_id,
        "turn_id": (task or {}).get("turn_id"),
    }
    return JSONResponse(status_code=202, content=payload)


@router.post("/generate-image", response_model=ImageGenerationResponse)
async def generate_image(
    body: ImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> ImageGenerationResponse:
    """Synchronous image generation — Gemini multi-turn / stateless edit / fresh.

    Pipeline (post-image-redesign):
      1. owner_scope from headers + body
      2. idempotency check on (owner_scope, client_request_id)
      3. expected_parent CAS pre-check
      4. resolve reference bytes (parent_artifact_id → reference_artifact_id
         → session.latest → reference_image[_url] legacy)
      5. style lock resolve
      6. provider call
      7. persist artifacts (raw + display + optional thumbnail)
      8. CAS-advance image_sessions.latest_artifact_id (when not branching)
      9. record image_turns row
     10. shape response (with optional `variants` map)
    """
    start_time = time.time()
    pool = _get_db_pool(request)
    artifact_storage = _get_artifact_storage()
    session_mgr = getattr(request.app.state, "session_manager", None)

    owner_scope = _resolve_owner_scope(user, body)
    turn_id = new_turn_id()

    try:
        _validate_single_explicit_reference(body)
        if _sync_uses_task_queue():
            return await _generate_image_via_task(
                body=body,
                request=request,
                user=user,
                model_registry=model_registry,
            )

        # ---- Idempotency -----------------------------------------------
        # Two-step: lookup → claim. The claim is INSERT-ON-CONFLICT-DO-
        # NOTHING; if it returns False, two concurrent same-key requests
        # raced and we lost. The winner already executed (or is mid-flight);
        # the sync surface has no cached body, so a same-hash replay returns
        # 409 ``duplicate_request_in_flight`` rather than re-charging.
        # Async path is the canonical idempotent surface.
        replay_task_id, idem_conflict, request_hash = await _check_idempotency(
            pool,
            owner_scope=owner_scope,
            body=body,
        )
        if idem_conflict:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "idempotency_conflict",
                    "message": "client_request_id reused with different request body",
                },
            )
        if replay_task_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "duplicate_request_in_flight",
                    "message": (
                        "Sync /generate-image does not cache prior response bodies; "
                        "use the async endpoint for true idempotent replay."
                    ),
                    "client_request_id": body.client_request_id,
                },
            )
        if body.client_request_id and request_hash and pool is not None:
            claimed = await record_idempotent(
                pool,
                owner_scope=owner_scope,
                client_request_id=body.client_request_id,
                request_hash=request_hash,
                task_id=turn_id,
            )
            if not claimed:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "duplicate_request_in_flight",
                        "message": (
                            "client_request_id raced with a concurrent submit on the sync surface."
                        ),
                        "client_request_id": body.client_request_id,
                    },
                )

        # ---- Provider routing pre-check --------------------------------
        model_info = model_registry.get_model(body.model_id) if model_registry else None
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id,
            selected_provider,
        )

        has_explicit_ref = bool(
            body.parent_artifact_id
            or body.reference_artifact_id
            or body.reference_blob_id
            or body.reference_image
            or body.reference_image_url
        )
        # session_id alone with a stored latest_artifact_id is also "has reference"
        # for routing purposes (we'll edit the prior image). We compute that
        # once here so routing decisions don't differ from persistence ones.
        session_implies_reference = False
        if body.session_id and pool is not None and not has_explicit_ref:
            sess_row = await get_image_session(pool, body.session_id)
            session_implies_reference = bool(sess_row and sess_row.get("latest_artifact_id"))
        has_reference = has_explicit_ref or session_implies_reference

        if has_reference and not prefer_gemini:
            return ImageGenerationResponse(
                success=False,
                images=[],
                provider=str(selected_provider or "unknown"),
                duration_ms=(time.time() - start_time) * 1000,
                error=(
                    f"reference image editing requires a Gemini model "
                    f"(got model_id={body.model_id!r}, provider={selected_provider!r}). "
                    "Drop the reference fields for fresh generation, or pick a "
                    "Gemini model_id (e.g. gemini-3-flash-preview)."
                ),
                error_code="reference_requires_gemini",
                client_request_id=body.client_request_id,
            )

        # ---- Ensure image_session row + concurrency check --------------
        if body.session_id:
            await _ensure_image_session(
                pool,
                session_id=body.session_id,
                owner_scope=owner_scope,
                user=user,
                body=body,
            )
            await _check_expected_parent(
                pool,
                session_id=body.session_id,
                expected_parent=body.expected_parent_artifact_id,
            )

        # ---- Style lock ------------------------------------------------
        style_explicit = "style" in body.model_fields_set
        effective_style, new_locked_style, clear_lock = await _resolve_style_for_session(
            pool,
            session_id=body.session_id,
            body_style=body.style,
            style_explicit=style_explicit,
        )

        width, height, aspect_ratio = parse_image_size(body.size)

        # ---- Resolve reference bytes ----------------------------------
        ref_b64: str | None = None
        resolved_parent: str | None = None
        if prefer_gemini and (has_explicit_ref or session_implies_reference):
            try:
                ref_b64, resolved_parent = await _resolve_reference_bytes(
                    body,
                    artifact_storage=artifact_storage,
                    user=user,
                    owner_scope=owner_scope,
                    db_pool=pool,
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    await _record_turn(
                        pool,
                        turn_id=turn_id,
                        session_id=body.session_id,
                        owner_scope=owner_scope,
                        task_id=None,
                        body=body,
                        parent_artifact_id=None,
                        output_artifact_id=None,
                        status="failed",
                        error="reference artifact not found",
                        error_code="reference_not_found",
                        request_hash=request_hash,
                    )
                raise

        # ---- Provider call --------------------------------------------
        styled_prompt = compose_styled_prompt(body.prompt, effective_style)
        provider_label: str | None = None

        if prefer_gemini and ref_b64 is not None:
            gemini = get_gemini_image_generator()
            if not gemini.is_configured:
                return ImageGenerationResponse(
                    success=False,
                    images=[],
                    provider="none",
                    duration_ms=(time.time() - start_time) * 1000,
                    error="Gemini API key not configured",
                    error_code="provider_unavailable",
                    client_request_id=body.client_request_id,
                    session_id=body.session_id,
                )
            async with _bounded(
                _provider_semaphore,
                status_code=429,
                error_code="provider_busy",
                message="Gemini image provider concurrency is saturated",
            ):
                res = await gemini.generate(
                    prompt=styled_prompt,
                    n=body.n,
                    aspect_ratio=aspect_ratio,
                    reference_image=ref_b64,
                )
            provider_label = "google"
        elif body.session_id and session_mgr and prefer_gemini:
            # No reference resolved but session+gemini → fall back to legacy
            # multi-turn path (history-backed). New behavior: when caller
            # has explicit parent_artifact_id we already resolved bytes above,
            # so this branch only fires for "session_id alone, never edited".
            res, session_state, err = await _run_gemini_multi_turn(
                body,
                aspect_ratio=aspect_ratio,
                width=width,
                height=height,
                session_manager=session_mgr,
                user=user,
                artifact_storage=artifact_storage,
            )
            if err:
                return ImageGenerationResponse(
                    success=False,
                    images=[],
                    provider="none",
                    duration_ms=(time.time() - start_time) * 1000,
                    error=err,
                    error_code="provider_unavailable",
                    session_id=body.session_id,
                    client_request_id=body.client_request_id,
                )
            if not (res and res.success and res.images):
                err_msg = (res.error if res else None) or "Image generation failed"
                return ImageGenerationResponse(
                    success=False,
                    images=[],
                    provider="google",
                    duration_ms=(
                        (res.duration_ms if res else None) or (time.time() - start_time) * 1000
                    ),
                    error=err_msg,
                    error_code="provider_failed",
                    session_id=body.session_id,
                    client_request_id=body.client_request_id,
                )
            # Persist via the legacy multi-turn helper (writes session metadata)
            raw_anchor, generated_list = await _persist_multi_turn_result(
                body,
                res=res,
                session_state=session_state,
                session_manager=session_mgr,
                user=user,
                artifact_storage=artifact_storage,
                width=width,
                height=height,
                owner_scope=owner_scope,
                turn_id=turn_id,
                provider="google",
                model_id=body.model_id,
                return_variants=body.return_variants,
                write_legacy_metadata=pool is None,
            )
            latest_advanced = await _post_generation_bookkeeping(
                pool,
                artifact_storage=artifact_storage,
                turn_id=turn_id,
                session_id=body.session_id,
                owner_scope=owner_scope,
                body=body,
                resolved_parent=resolved_parent,
                raw_anchor=raw_anchor,
                request_hash=request_hash,
                new_locked_style=new_locked_style,
                clear_lock=clear_lock,
            )
            return ImageGenerationResponse(
                success=True,
                images=generated_list,
                provider="google",
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                session_id=body.session_id,
                turn_id=turn_id,
                parent_artifact_id=resolved_parent,
                output_artifact_id=raw_anchor,
                client_request_id=body.client_request_id,
                idempotent_replay=False,
                latest_advanced=latest_advanced,
                variants=await _build_variants_response(
                    artifact_storage,
                    raw_artifact_id=raw_anchor,
                    return_variants=body.return_variants,
                    owner_scope=owner_scope,
                ),
            )
        else:
            dashscope_tag = resolve_dashscope_style_tag(effective_style)
            negative_prompt = resolve_negative_prompt(effective_style)
            router_svc = get_smart_image_generator()
            async with _bounded(
                _provider_semaphore,
                status_code=429,
                error_code="provider_busy",
                message="Image provider concurrency is saturated",
            ):
                res = await router_svc.generate(
                    prompt=styled_prompt,
                    n=body.n,
                    size=body.size or "1536*1536",
                    style=dashscope_tag,
                    negative_prompt=negative_prompt,
                    aspect_ratio=aspect_ratio,
                    prefer_gemini=prefer_gemini,
                    prefer_doubao=prefer_doubao,
                    dashscope_model=dashscope_model,
                )
            provider_label = res.provider

        _cap_result_images(res, body.n)
        if not res.success or not res.images:
            err = res.error or "Image generation failed"
            error_code = "provider_failed"
            if res.blocked and res.block_reason:
                err = f"{err} (blocked: {res.block_reason})"
                error_code = "provider_blocked"
            await _record_turn(
                pool,
                turn_id=turn_id,
                session_id=body.session_id,
                owner_scope=owner_scope,
                task_id=None,
                body=body,
                parent_artifact_id=resolved_parent,
                output_artifact_id=None,
                status="failed",
                error=err,
                error_code=error_code,
                request_hash=request_hash,
            )
            return ImageGenerationResponse(
                success=False,
                images=[],
                provider=provider_label,
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                error=err,
                error_code=error_code,
                session_id=body.session_id,
                client_request_id=body.client_request_id,
            )

        # ---- Persist artifacts ----------------------------------------
        persisted = await asyncio.gather(
            *[
                _persist_and_get_url_bounded(
                    img,
                    artifact_storage=artifact_storage,
                    session_id=body.session_id,
                    user=user,
                    prompt=body.prompt,
                    add_watermark=body.add_watermark,
                    width=width,
                    height=height,
                    index=i,
                    owner_scope=owner_scope,
                    turn_id=turn_id,
                    parent_artifact_id=resolved_parent,
                    provider=provider_label,
                    model_id=body.model_id,
                    return_variants=body.return_variants,
                )
                for i, img in enumerate(res.images)
            ]
        )
        raw_anchor = persisted[0][0] if persisted else None
        generated_list = [gi for _, gi in persisted]

        # ---- CAS advance + style lock + turn audit + idempotency claim
        latest_advanced = await _post_generation_bookkeeping(
            pool,
            artifact_storage=artifact_storage,
            turn_id=turn_id,
            session_id=body.session_id,
            owner_scope=owner_scope,
            body=body,
            resolved_parent=resolved_parent,
            raw_anchor=raw_anchor,
            request_hash=request_hash,
            new_locked_style=new_locked_style,
            clear_lock=clear_lock,
        )

        return ImageGenerationResponse(
            success=True,
            images=generated_list,
            provider=provider_label,
            duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
            session_id=body.session_id,
            turn_id=turn_id,
            parent_artifact_id=resolved_parent,
            output_artifact_id=raw_anchor,
            client_request_id=body.client_request_id,
            idempotent_replay=False,
            latest_advanced=latest_advanced,
            variants=await _build_variants_response(
                artifact_storage,
                raw_artifact_id=raw_anchor,
                return_variants=body.return_variants,
                owner_scope=owner_scope,
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        record_internal_exception(__name__, "assistant.api.routes.images.internal_failure", e)
        # Best-effort failure-record so callers can audit via /image-sessions
        try:
            await _record_turn(
                pool,
                turn_id=turn_id,
                session_id=body.session_id,
                owner_scope=owner_scope,
                task_id=None,
                body=body,
                parent_artifact_id=None,
                output_artifact_id=None,
                status="failed",
                error=str(e),
                error_code="internal_error",
                request_hash=None,
            )
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.api.routes.images.suppressed_failure",
                exc,
                level=logging.DEBUG,
            )
        return ImageGenerationResponse(
            success=False,
            images=[],
            provider="unknown",
            duration_ms=(time.time() - start_time) * 1000,
            error=str(e),
            error_code="internal_error",
            session_id=body.session_id,
            client_request_id=body.client_request_id,
        )


async def _post_generation_bookkeeping(
    pool,
    *,
    artifact_storage,
    turn_id: str,
    session_id: str | None,
    owner_scope: str,
    body: ImageGenerationRequest,
    resolved_parent: str | None,
    raw_anchor: str | None,
    request_hash: str | None,
    new_locked_style: str | None,
    clear_lock: bool = False,
) -> bool:
    """Returns ``latest_advanced``: True iff session.latest_artifact_id
    actually advanced to ``raw_anchor`` (or no session was involved). False
    iff the CAS lost a race to a concurrent racer — caller MUST surface
    this to the response (``latest_advanced=False``) so the client knows
    its output is a branch, not the new latest."""
    # Style lock write
    if session_id and pool is not None:
        if new_locked_style:
            try:
                await _set_locked_style(pool, session_id, new_locked_style)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.images.internal_failure", exc
                )
        elif clear_lock:
            try:
                await _set_locked_style(pool, session_id, None)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.images.internal_failure", exc
                )

    # CAS advance
    latest_advanced = True  # default for no-session / allow_branch cases
    if session_id and raw_anchor and pool is not None and not body.allow_branch:
        try:
            latest_advanced = await advance_latest_artifact_cas(
                pool,
                session_id=session_id,
                expected_parent=resolved_parent,
                new_artifact_id=raw_anchor,
            )
            if not latest_advanced:
                logger.info(
                    "latest_artifact CAS lost race session=%s parent=%s — output is a branch",
                    session_id,
                    resolved_parent,
                )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.images.internal_failure", exc
            )
            latest_advanced = False
    elif session_id and body.allow_branch:
        # Caller explicitly asked for a branch — don't advance, don't claim.
        latest_advanced = False

    # Turn audit
    try:
        await _record_turn(
            pool,
            turn_id=turn_id,
            session_id=session_id,
            owner_scope=owner_scope,
            task_id=None,
            body=body,
            parent_artifact_id=resolved_parent,
            output_artifact_id=raw_anchor,
            status="completed",
            error=None,
            error_code=None,
            request_hash=request_hash,
            output_artifact_ids=[raw_anchor] if raw_anchor else None,
            state="completed",
        )
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.images.internal_failure", exc
        )

    # NOTE: idempotency claim was made BEFORE generation in the route
    # handlers (sync + async paths). Bookkeeping no longer claims —
    # otherwise a worker crash mid-generation would race with the route
    # claim. Single source of truth = pre-work claim.

    return latest_advanced


# -----------------------------------------------------------------------------
# POST /generate-image-async + GET /image-task/{task_id}
# -----------------------------------------------------------------------------


async def _run_image_generation_task(
    task_id: str,
    body: AsyncImageGenerationRequest,
    model_registry: ModelRegistry,
    user: UserContext,
    session_manager=None,
    redis=None,
    pool=None,
) -> None:
    bindings = ImageWorkerBindings(
        load_task=_load_task,
        store_task=_store_task,
        resolve_owner_scope=_resolve_owner_scope,
        update_turn_status=update_turn_status,
        get_artifact_storage=_get_artifact_storage,
        get_image_session=get_image_session,
        resolve_reference_bytes=_resolve_reference_bytes,
        resolve_style_for_session=_resolve_style_for_session,
        run_gemini_multi_turn=_run_gemini_multi_turn,
        get_gemini_image_generator=get_gemini_image_generator,
        get_smart_image_generator=get_smart_image_generator,
        bounded=_bounded,
        provider_semaphore=_provider_semaphore,
        cap_result_images=_cap_result_images,
        persist_multi_turn_result=_persist_multi_turn_result,
        post_generation_bookkeeping=_post_generation_bookkeeping,
        persist_and_get_url_bounded=_persist_and_get_url_bounded,
        send_image_callback=send_image_callback,
    )
    await run_image_generation_task(
        task_id,
        body,
        model_registry,
        user,
        session_manager=session_manager,
        redis=redis,
        pool=pool,
        bindings=bindings,
    )


@router.post("/generate-image-async", response_model=AsyncImageTaskSubmitResponse)
async def submit_image_generation(
    body: AsyncImageGenerationRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    model_registry: ModelRegistry = Depends(get_model_registry),
) -> AsyncImageTaskSubmitResponse:
    _cleanup_old_tasks()
    _validate_single_explicit_reference(body)
    pool = _get_db_pool(request)
    if pool is None and _requires_persistent_task_store():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "db_unavailable",
                "message": "Image task database is unavailable",
            },
        )
    owner_scope = _resolve_owner_scope(user, body)
    total_active = await count_active_image_tasks(pool, owner_scope=None)
    if total_active is not None and total_active >= _MAX_QUEUE_DEPTH:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "queue_full",
                "message": "Image task queue is full",
                "retry_after": 5,
            },
        )
    owner_active = await count_active_image_tasks(pool, owner_scope=owner_scope)
    if owner_active is not None and owner_active >= _MAX_OWNER_ACTIVE_TASKS:
        raise HTTPException(
            status_code=429,
            detail={
                "error_code": "owner_concurrency_limit",
                "message": "Too many active image tasks for this owner",
                "retry_after": 5,
            },
        )

    # Idempotency check before any work
    replay_task_id, idem_conflict, request_hash = await _check_idempotency(
        pool,
        owner_scope=owner_scope,
        body=body,
    )
    if idem_conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "idempotency_conflict",
                "message": "client_request_id reused with different request body",
            },
        )
    if replay_task_id:
        task = await _load_task_any(getattr(request.app.state, "redis", None), pool, replay_task_id)
        return AsyncImageTaskSubmitResponse(
            task_id=replay_task_id,
            status=(task or {}).get("status", "pending"),
            message="Idempotent replay — existing task returned",
        )

    # Image-session bootstrap + expected_parent CAS pre-check
    if body.session_id:
        await _ensure_image_session(
            pool,
            session_id=body.session_id,
            owner_scope=owner_scope,
            user=user,
            body=body,
        )
        await _check_expected_parent(
            pool,
            session_id=body.session_id,
            expected_parent=body.expected_parent_artifact_id,
        )

    task_id = str(uuid.uuid4())
    turn_id = new_turn_id()
    now = datetime.now(timezone.utc).isoformat()
    task = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "prompt": body.prompt,
        "model_id": body.model_id,
        "provider": None,
        "images": [],
        "duration_ms": None,
        "error": None,
        "error_code": None,
        "created_at": now,
        "completed_at": None,
        "owner_user_id": user.user_id,
        "owner_tenant_id": user.tenant_id,
        "owner_scope": owner_scope,
        "turn_id": turn_id,
        "session_id": body.session_id,
        "client_request_id": body.client_request_id,
        "request_hash": request_hash,
        "parent_artifact_id": None,
        "output_artifact_id": None,
    }
    # Claim idempotency BEFORE scheduling any work. ``record_idempotent``
    # is a Postgres ON CONFLICT DO NOTHING insert — when two concurrent
    # submits race with the same client_request_id+payload, exactly one
    # wins the insert. The loser must NOT spawn another worker; instead
    # it looks up the winner's task_id and returns it as a replay.
    if body.client_request_id and request_hash and pool is not None:
        # ``record_idempotent`` raises on real Postgres errors (no longer
        # wrapped in _db_safe) — those propagate to the outer try/except
        # → 500 → caller retries → still safe.
        claimed = await record_idempotent(
            pool,
            owner_scope=owner_scope,
            client_request_id=body.client_request_id,
            request_hash=request_hash,
            task_id=task_id,
        )
        if not claimed:
            existing = await lookup_idempotent(
                pool,
                owner_scope=owner_scope,
                client_request_id=body.client_request_id,
            )
            if existing and existing["request_hash"] == request_hash:
                task = await _load_task_any(
                    getattr(request.app.state, "redis", None),
                    pool,
                    existing["task_id"],
                )
                return AsyncImageTaskSubmitResponse(
                    task_id=existing["task_id"],
                    status=(task or {}).get("status", "pending"),
                    message="Idempotent replay — existing task returned",
                )
            if existing and existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "idempotency_conflict",
                        "message": "client_request_id raced with a different request body",
                    },
                )
            # claimed=False AND no row found = impossible-by-construction
            # if both calls hit the same DB. If we ever land here it means
            # something outside our model violated invariants — refuse to
            # run rather than risk double-spend.
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "internal_error",
                    "message": (
                        "idempotency state is inconsistent (insert failed but "
                        "no existing row found) — please retry"
                    ),
                },
            )

    redis = getattr(request.app.state, "redis", None)
    try:
        await create_image_task(
            pool,
            task_id=task_id,
            owner_scope=owner_scope,
            status="pending",
            prompt=body.prompt,
            model_id=body.model_id,
            request_payload=body.model_dump(mode="json"),
            progress=0,
            turn_id=turn_id,
            session_id=body.session_id,
            client_request_id=body.client_request_id,
            request_hash=request_hash,
        )
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.images.internal_failure", exc
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "task_store_unavailable",
                "message": "Image task store is unavailable; please retry",
            },
        ) from exc
    await _store_task(redis, task_id, task, pool=pool)

    # Insert pending turn row so a poll between submit + worker start sees
    # the queue state.
    try:
        await insert_turn(
            pool,
            turn_id=turn_id,
            session_id=body.session_id or f"stateless_{turn_id}",
            owner_scope=owner_scope,
            task_id=task_id,
            prompt=body.prompt,
            model_id=body.model_id,
            style=getattr(body.style, "value", None),
            add_watermark=body.add_watermark,
            parent_artifact_id=None,
            output_artifact_id=None,
            status="pending",
            error=None,
            error_code=None,
            client_request_id=body.client_request_id,
            request_hash=request_hash,
        )
        logger.info(
            "insert_turn succeeded: turn_id=%s task_id=%s session_id=%s",
            turn_id,
            task_id,
            body.session_id,
        )
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.images.internal_failure", exc
        )

    session_mgr = get_session_manager(request)
    worker = asyncio.create_task(
        _run_image_generation_task(
            task_id,
            body,
            model_registry,
            user,
            session_manager=session_mgr,
            redis=redis,
            pool=pool,
        )
    )
    _in_flight_workers.add(worker)
    worker.add_done_callback(_in_flight_workers.discard)
    return AsyncImageTaskSubmitResponse(
        task_id=task_id,
        status="pending",
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

    # Cache miss → fall through to image_turns row (Postgres). Both Redis
    # and the in-process dict expire entries after 1h; image_turns is
    # authoritative beyond that window.
    if not task:
        pool = _get_db_pool(request)
        if pool is not None:
            task_row = await get_image_task(pool, task_id)
            if task_row:
                task = _task_from_db_row(task_row)
                if task:
                    task.setdefault("owner_scope", task_row.get("owner_scope"))
                    # Continue through the normal task response path below.
            if not task:
                turn = await get_turn_by_task(pool, task_id)
                if turn:
                    if turn.get("owner_scope") != _resolve_owner_scope_from_user(user):
                        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
                    # Build a synthetic task dict from the turn row
                    images: list[AsyncImageArtifact] = []
                    output_id = turn.get("output_artifact_id")
                    if output_id:
                        artifact_storage = _get_artifact_storage()
                        if artifact_storage:
                            try:
                                (
                                    url,
                                    actual,
                                ) = await artifact_storage.get_presigned_download_url_for_variant(
                                    output_id,
                                    "display",
                                    owner_scope=turn.get("owner_scope"),
                                    tenant_id=user.tenant_id,
                                    user_id=user.user_id,
                                )
                            except Exception as exc:
                                record_internal_exception(
                                    __name__,
                                    "assistant.api.routes.images.internal_failure",
                                    exc,
                                )
                                url, actual = None, None
                            if url:
                                public_id = output_id
                                if actual and actual != "raw":
                                    try:
                                        public_artifact = await artifact_storage.find_variant(
                                            output_id, actual
                                        )
                                        public_id = getattr(
                                            public_artifact, "artifact_id", output_id
                                        )
                                    except Exception as exc:
                                        record_internal_exception(
                                            __name__,
                                            "assistant.api.routes.images.internal_failure",
                                            exc,
                                        )
                                        public_id = output_id
                                images = [
                                    AsyncImageArtifact(
                                        artifact_id=public_id,
                                        download_url=url,
                                        url=url,
                                    )
                                ]
                    completed_at = turn.get("completed_at")
                    created_at = turn.get("created_at")
                    return AsyncImageTaskStatusResponse(
                        task_id=task_id,
                        status=turn.get("status", "pending"),
                        progress=100 if turn.get("status") in ("completed", "failed") else 50,
                        prompt=turn.get("prompt") or "",
                        model_id=turn.get("model_id") or "",
                        provider=None,
                        images=images,
                        duration_ms=None,
                        error=turn.get("error"),
                        error_code=turn.get("error_code"),
                        created_at=(created_at.isoformat() if created_at else ""),
                        completed_at=(completed_at.isoformat() if completed_at else None),
                        turn_id=turn.get("turn_id"),
                        session_id=turn.get("session_id"),
                        parent_artifact_id=turn.get("parent_artifact_id"),
                        output_artifact_id=output_id,
                        client_request_id=turn.get("client_request_id"),
                    )
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if not _task_is_visible_to_user(task, user):
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
        error_code=task.get("error_code"),
        created_at=task["created_at"],
        completed_at=task.get("completed_at"),
        turn_id=task.get("turn_id"),
        session_id=task.get("session_id"),
        parent_artifact_id=task.get("parent_artifact_id"),
        output_artifact_id=task.get("output_artifact_id"),
        client_request_id=task.get("client_request_id"),
        latest_advanced=task.get("latest_advanced"),
    )


# -----------------------------------------------------------------------------
# GET /artifacts/{artifact_id}/download-url  — presigned URL for a variant
# -----------------------------------------------------------------------------


@router.get(
    "/artifacts/{artifact_id}/download-url",
    response_model=ArtifactDownloadUrlResponse,
)
async def get_artifact_download_url(
    artifact_id: str,
    request: Request,
    variant: str = Query("display", description="raw | display | thumbnail"),
    expires_in: int = Query(3600, ge=60, le=3600),
    user: UserContext = Depends(get_user_context),
) -> ArtifactDownloadUrlResponse:
    """Resolve a fresh presigned URL for any variant of an image artifact.

    Variant fallback chain:
      * thumbnail → display → raw
      * display   → raw
      * raw       → no fallback (404 if missing)

    Owner scope is enforced. Cross-owner reads return 404 (not 403) to
    avoid IDOR enumeration.
    """
    if variant not in ("raw", "display", "thumbnail"):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "validation_error",
                "message": "variant must be one of raw|display|thumbnail",
            },
        )

    artifact_storage = _get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "storage_unavailable",
                "message": "ArtifactStorage not configured",
            },
        )

    owner_scope = _resolve_owner_scope_from_user(user)
    url, actual_variant = await artifact_storage.get_presigned_download_url_for_variant(
        artifact_id,
        variant,
        expiry_seconds=expires_in,
        owner_scope=owner_scope,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if url is None or actual_variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found", "message": f"artifact {artifact_id!r} not found"},
        )

    # Pull metadata for width/height/mime
    artifact = await artifact_storage.find_variant(artifact_id, actual_variant)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    return ArtifactDownloadUrlResponse(
        artifact_id=artifact_id,
        variant=actual_variant,
        url=url,
        expires_at=expires_at,
        width=artifact.width if artifact else None,
        height=artifact.height if artifact else None,
        mime_type=artifact.mime_type if artifact else None,
    )


# -----------------------------------------------------------------------------
# GET /image-sessions/{session_id} — multi-turn history
# -----------------------------------------------------------------------------


@router.get(
    "/image-sessions/{session_id}",
    response_model=ImageSessionResponse,
)
async def get_image_session_view(
    session_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    include_urls: bool = Query(False),
    user: UserContext = Depends(get_user_context),
) -> ImageSessionResponse:
    """Return the focused image-multi-turn view for a session.

    * Owner-scoped: cross-owner sessions return 404.
    * Pagination: cursor is "ISO_created_at|turn_id" — older-than ordering.
    * ``include_urls=true`` attaches a presigned display URL per turn (3600 s).
    """
    pool = _get_db_pool(request)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "storage_unavailable", "message": "Database pool not available"},
        )

    sess = await get_image_session(pool, session_id)
    if not sess:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "not_found",
                "message": f"image session {session_id!r} not found",
            },
        )

    requested_owner_scope = _resolve_owner_scope_from_user(user)
    session_owner_scope = sess.get("owner_scope")
    if session_owner_scope and session_owner_scope != requested_owner_scope:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "not_found",
                "message": f"image session {session_id!r} not found",
            },
        )
    rows, next_cursor = await list_turns(
        pool,
        session_id=session_id,
        owner_scope=requested_owner_scope,
        limit=limit,
        cursor=cursor,
    )

    artifact_storage = _get_artifact_storage() if include_urls else None
    turns_out: list[ImageTurnPublic] = []
    for row in rows:
        output_url: str | None = None
        oid = row.get("output_artifact_id")
        if include_urls and artifact_storage and oid:
            try:
                url, _ = await artifact_storage.get_presigned_download_url_for_variant(
                    oid,
                    "display",
                    owner_scope=requested_owner_scope,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                )
                output_url = url
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.images.internal_failure", exc
                )
        created_at = row.get("created_at")
        completed_at = row.get("completed_at")
        turns_out.append(
            ImageTurnPublic(
                turn_id=row["turn_id"],
                task_id=row.get("task_id"),
                prompt=row.get("prompt"),
                model_id=row.get("model_id"),
                style=row.get("style"),
                add_watermark=bool(row.get("add_watermark", True)),
                parent_artifact_id=row.get("parent_artifact_id"),
                output_artifact_id=oid,
                status=row.get("status", "unknown"),
                error=row.get("error"),
                error_code=row.get("error_code"),
                created_at=created_at.isoformat() if created_at else "",
                completed_at=completed_at.isoformat() if completed_at else None,
                output_url=output_url,
            )
        )

    sess_created = sess.get("created_at")
    sess_updated = sess.get("updated_at")
    return ImageSessionResponse(
        session_id=session_id,
        latest_artifact_id=sess.get("latest_artifact_id"),
        locked_style=sess.get("locked_style"),
        created_at=sess_created.isoformat() if sess_created else "",
        updated_at=sess_updated.isoformat() if sess_updated else "",
        turns=turns_out,
        next_cursor=next_cursor,
    )
