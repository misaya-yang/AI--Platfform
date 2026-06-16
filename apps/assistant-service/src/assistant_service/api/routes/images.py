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
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.image import (
    advance_latest_artifact_cas,
    append_image_turns,
    apply_watermark_b64,
    build_gemini_contents_from_history,
    count_active_image_tasks,
    create_image_blob,
    create_image_task,
    get_image_blob,
    get_image_session,
    get_image_task,
    get_turn_by_task,
    inflate_history_with_bytes,
    insert_turn,
    list_turns,
    lookup_idempotent,
    make_thumbnail,
    new_turn_id,
    parse_image_size,
    record_idempotent,
    resolve_image_routing,
    send_image_callback,
    update_image_blob_status,
    update_image_task,
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
from ai_gateway_core.security import SafeFetchError, safe_fetch
from ai_gateway_core.style_presets import (
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ...auth import UserContext, get_user_context
from ...core.models.model_registry import ModelRegistry
from ...core.tools.gemini_image_tool import get_gemini_image_generator
from ...core.tools.smart_image_generator import get_smart_image_generator
from ..deps import get_model_registry, get_session_manager

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
    env = (
        os.getenv("ENVIRONMENT")
        or os.getenv("APP_ENV")
        or os.getenv("ENV")
        or ""
    ).lower()
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
    size: str | None = "1536*1536"
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
    reference_blob_id: str | None = Field(
        default=None,
        description=(
            "Object-store blob id created by /image-blobs/upload-url, "
            "/image-blobs/complete, or /image-blobs/fetch-url. Preferred for "
            "user-provided reference uploads because request bodies stay small."
        ),
    )
    reference_image: str | None = Field(
        default=None,
        max_length=12_000_000,
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

    # ------------- Image-redesign Phase 2 — multi-turn primitives -------------
    app_user_id: str | None = Field(
        default=None,
        description=(
            "End-user id when the API caller is a multi-tenant app proxying "
            "for its own users. Combined with `app_tenant_id` and the JWT "
            "subject to compute owner_scope; isolates artifacts per end-user."
        ),
    )
    app_tenant_id: str | None = Field(
        default=None,
        description=(
            "Tenant id of the calling app's end-user. See `app_user_id`."
        ),
    )
    parent_artifact_id: str | None = Field(
        default=None,
        description=(
            "Explicit anchor for next-turn editing. When set, we use this "
            "artifact's raw bytes as the reference image and lineage parent. "
            "Overrides session_id-derived latest_artifact lookup. Owner-scoped."
        ),
    )
    expected_parent_artifact_id: str | None = Field(
        default=None,
        description=(
            "Optimistic-concurrency check. When set, verifies the session's "
            "current latest_artifact_id equals this value before generating; "
            "409 latest_artifact_conflict on mismatch."
        ),
    )
    client_request_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Idempotency key. Same (owner_scope, client_request_id) + same "
            "request body → returns the original task. Different body → 409 "
            "idempotency_conflict."
        ),
    )
    return_variants: list[str] | None = Field(
        default=None,
        description=(
            "Optional: extra variants to include in the response's `variants` "
            "map. Subset of: 'raw' | 'display' | 'thumbnail'. The default "
            "`images[].url` continues to be the display URL (raw when "
            "watermark disabled)."
        ),
    )
    allow_branch: bool = Field(
        default=False,
        description=(
            "When True, generating from a non-latest parent does NOT advance "
            "latest_artifact_id (creates a sibling branch). Default False = "
            "advance only when parent matches current latest."
        ),
    )

    @field_validator("style", mode="before")
    @classmethod
    def _coerce_style(cls, value: Any) -> StylePreset:
        if isinstance(value, StylePreset):
            return value
        return resolve_style_preset(value)


class ImageGenerationResponse(BaseModel):
    success: bool
    images: list[GeneratedImage] = []
    task_id: str | None = Field(
        default=None,
        description="Task id when sync generation was queued or replayed.",
    )
    status: str | None = Field(
        default=None,
        description="Task status when sync generation returns before completion.",
    )
    provider: str | None = None
    duration_ms: float | None = None
    error: str | None = None
    error_code: str | None = Field(
        default=None,
        description=(
            "Machine-readable error code when success=False. Examples: "
            "idempotency_conflict, latest_artifact_conflict, reference_not_found, "
            "provider_blocked, provider_unavailable, validation_error."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description="Echo of session_id when stateful multi-turn was used.",
    )
    turn_id: str | None = Field(
        default=None,
        description="Stable identifier for this turn (image_turns.turn_id).",
    )
    parent_artifact_id: str | None = Field(
        default=None,
        description="Resolved parent artifact_id we generated against, if any.",
    )
    output_artifact_id: str | None = Field(
        default=None,
        description=(
            "The raw output artifact_id — the canonical lineage anchor for "
            "the next turn. Same value advances `latest_artifact_id` on the "
            "image_session row when CAS succeeds."
        ),
    )
    client_request_id: str | None = Field(
        default=None,
        description="Echo of client_request_id when supplied.",
    )
    idempotent_replay: bool = Field(
        default=False,
        description=(
            "True when this response was served from idempotency replay "
            "(matched (owner_scope, client_request_id) + request_hash)."
        ),
    )
    variants: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional variant→URL map populated when caller passes "
            "`return_variants`. Includes only resolvable variants."
        ),
    )
    latest_advanced: bool = Field(
        default=True,
        description=(
            "True when ``image_sessions.latest_artifact_id`` was advanced "
            "to ``output_artifact_id``. False when the CAS lost a race or "
            "the caller passed ``allow_branch=true`` — the output exists "
            "as a branch, not the new latest. Clients that want to keep "
            "editing should re-fetch the session before submitting next "
            "turn."
        ),
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
    error_code: str | None = None
    created_at: str
    completed_at: str | None = None
    # Image-redesign Phase 2 fields (all optional for back-compat)
    turn_id: str | None = None
    session_id: str | None = None
    parent_artifact_id: str | None = None
    output_artifact_id: str | None = None
    client_request_id: str | None = None
    latest_advanced: bool | None = None


class ImageBlobUploadUrlRequest(BaseModel):
    filename: str = Field(default="reference.png", max_length=255)
    mime_type: str = Field(default="image/png")
    byte_size: int | None = Field(default=None, ge=1)
    content_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class ImageBlobUploadUrlResponse(BaseModel):
    blob_id: str
    upload_url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, str] | None = None
    storage_key: str
    expires_at: str


class ImageBlobCompleteRequest(BaseModel):
    blob_id: str
    content_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    byte_size: int | None = Field(default=None, ge=1)
    mime_type: str | None = None


class ImageBlobResponse(BaseModel):
    blob_id: str
    status: str
    content_sha256: str | None = None
    byte_size: int | None = None
    mime_type: str
    storage_key: str


class ImageBlobFetchUrlRequest(BaseModel):
    url: str
    mime_type: str | None = None


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


async def _store_task(redis, task_id: str, task: dict, *, pool=None) -> None:
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
            await _persist_task_state(pool, task)
            return
        except Exception as exc:
            logger.warning("Redis task store write failed (%s); falling back to dict", exc)
    _image_tasks[task_id] = task
    await _persist_task_state(pool, task)


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


async def _persist_task_state(pool, task: dict, *, lock_seconds: int | None = None) -> None:
    try:
        await update_image_task(
            pool,
            task_id=task["task_id"],
            status=task.get("status"),
            progress=task.get("progress"),
            provider=task.get("provider"),
            result=task,
            error=task.get("error"),
            error_code=task.get("error_code"),
            parent_artifact_id=task.get("parent_artifact_id"),
            output_artifact_id=task.get("output_artifact_id"),
            locked_seconds=lock_seconds,
        )
    except Exception as exc:
        logger.warning("image task DB state update failed task=%s: %s", task.get("task_id"), exc)


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


def _get_storage_backend(artifact_storage):
    backend = getattr(artifact_storage, "_backend", None)
    if backend is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "storage_unavailable",
                "message": "Object storage backend is not configured",
            },
        )
    return backend


def _validate_image_mime(mime_type: str | None) -> str:
    mt = (mime_type or "image/png").split(";", 1)[0].strip().lower()
    if not mt.startswith("image/"):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "validation_error",
                "message": "mime_type must be an image/* media type",
            },
        )
    return mt


def _blob_storage_key(owner_scope: str, blob_id: str, filename: str) -> str:
    owner_hash = hashlib.sha256(owner_scope.encode("utf-8")).hexdigest()[:24]
    safe_name = filename.replace("/", "_").replace("\\", "_").replace("\x00", "")
    safe_name = safe_name or "reference.png"
    return f"image-blobs/{owner_hash}/{blob_id}/{safe_name[:180]}"


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalize_reference_image_b64(value: str) -> tuple[str, bytes, str]:
    mime_type = "image/png"
    raw = value.strip()
    if raw.startswith("data:"):
        header, sep, data = raw.partition(",")
        if not sep:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "validation_error", "message": "Invalid data URL reference_image"},
            )
        if ";" in header:
            mime_type = header[5:].split(";", 1)[0] or mime_type
        raw = data
    try:
        decoded = _b64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "validation_error", "message": "reference_image must be base64"},
        ) from exc
    if len(decoded) > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "reference_too_large",
                "message": f"reference image exceeds {_REFERENCE_MAX_BYTES} bytes",
            },
        )
    return _b64.b64encode(decoded).decode(), decoded, _validate_image_mime(mime_type)


def _validate_single_explicit_reference(body: ImageGenerationRequest) -> None:
    refs = {
        "parent_artifact_id": body.parent_artifact_id,
        "reference_artifact_id": body.reference_artifact_id,
        "reference_blob_id": body.reference_blob_id,
        "reference_image": body.reference_image,
        "reference_image_url": body.reference_image_url,
    }
    present = [name for name, value in refs.items() if value]
    if len(present) > 1:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "multiple_reference_sources",
                "message": (
                    "Only one explicit reference source may be supplied: "
                    "parent_artifact_id, reference_artifact_id, reference_blob_id, "
                    "reference_image, or reference_image_url"
                ),
                "fields": present,
            },
        )


async def _store_blob_bytes(
    *,
    pool,
    artifact_storage,
    owner_scope: str,
    user: UserContext,
    content: bytes,
    mime_type: str,
    source: str,
    filename: str = "reference.png",
) -> dict[str, Any]:
    if len(content) > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error_code": "reference_too_large",
                "message": f"reference image exceeds {_REFERENCE_MAX_BYTES} bytes",
            },
        )
    backend = _get_storage_backend(artifact_storage)
    blob_id = f"iblob_{uuid.uuid4().hex[:24]}"
    storage_key = _blob_storage_key(owner_scope, blob_id, filename)
    mt = _validate_image_mime(mime_type)
    sha = _sha256_hex(content)
    await backend.upload(
        storage_key,
        content,
        mt,
        metadata={
            "owner-scope-sha256": hashlib.sha256(owner_scope.encode("utf-8")).hexdigest(),
            "source": source,
            "blob-id": blob_id,
        },
    )
    await create_image_blob(
        pool,
        blob_id=blob_id,
        owner_scope=owner_scope,
        content_sha256=sha,
        byte_size=len(content),
        mime_type=mt,
        storage_key=storage_key,
        source=source,
        status="ready",
        artifact_id=None,
    )
    return {
        "blob_id": blob_id,
        "status": "ready",
        "content_sha256": sha,
        "byte_size": len(content),
        "mime_type": mt,
        "storage_key": storage_key,
        "artifact_id": None,
    }


async def _download_blob_bytes(
    *,
    pool,
    artifact_storage,
    blob_id: str,
    owner_scope: str,
) -> bytes:
    row = await get_image_blob(pool, blob_id=blob_id, owner_scope=owner_scope)
    if not row or row.get("status") != "ready":
        raise HTTPException(
            status_code=404,
            detail={"error_code": "reference_not_found", "message": f"blob {blob_id!r} not found"},
        )
    artifact_id = row.get("artifact_id")
    if artifact_id:
        content = await artifact_storage.download_artifact(artifact_id)
    else:
        backend = _get_storage_backend(artifact_storage)
        content = await backend.download(row["storage_key"])
    if not content:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "reference_not_found", "message": f"blob {blob_id!r} not found"},
        )
    if len(content) > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error_code": "reference_too_large", "message": "reference blob exceeds size limit"},
        )
    expected_sha = row.get("content_sha256")
    if expected_sha and _sha256_hex(content) != expected_sha:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "blob_integrity_mismatch", "message": "reference blob checksum mismatch"},
        )
    return content


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
    owner_scope: str | None = None,
    turn_id: str | None = None,
    parent_artifact_id: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    return_variants: list[str] | None = None,
) -> tuple[str | None, GeneratedImage]:
    """Persist artifact(s) and return (raw_artifact_id_for_history, public_GeneratedImage).

    Persists up to three artifacts per generated image:

    * **raw** (always) — un-watermarked; lineage anchor for next-turn editing
      and the artifact whose id the response's ``output_artifact_id`` carries.
    * **display** (when ``add_watermark=True``) — watermarked; the URL we
      return as ``images[].url``.
    * **thumbnail** (when ``return_variants`` includes 'thumbnail' OR Pillow
      downscale succeeds) — small (≤256 px longest edge) — for previews.

    All variant rows reference the raw artifact_id via ``parent_artifact_id``
    so the ``find_variant`` lookup walks both directions.

    The ``GeneratedImage.url`` we return is:
      * the watermarked display URL when ``add_watermark`` and persist
        succeeded;
      * else the raw URL;
      * else a data: URL fallback.
    The ``GeneratedImage.artifact_id`` we return is **the display variant's
    id** when watermarking succeeded (preserves the historical contract
    where downstream apps treat it as the public id) — but the *raw*
    artifact_id (first tuple element) remains the lineage anchor for
    multi-turn replay and the response's ``output_artifact_id``.
    """
    raw_b64 = img.get("content_base64", "")
    raw_mt = img.get("mime_type", "image/png")

    if not artifact_storage or not raw_b64:
        # Fallback path: data URL response, no artifacts.
        if add_watermark and raw_b64:
            cb64, mt = await asyncio.to_thread(apply_watermark_b64, raw_b64)
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
            variant="raw",
            parent_artifact_id=parent_artifact_id,
            turn_id=turn_id,
            owner_scope=owner_scope,
            width=width,
            height=height,
            provider=provider,
            model_id=model_id,
            prompt=prompt,
        )
        raw_artifact_id = raw_artifact.artifact_id
        raw_url = await artifact_storage.get_presigned_download_url(raw_artifact)
    except Exception as e:
        logger.warning("Failed to save raw image artifact: %s", e)
        # Fall back to data URL as response, no artifact.
        if add_watermark:
            cb64, mt = await asyncio.to_thread(apply_watermark_b64, raw_b64)
        else:
            cb64, mt = raw_b64, raw_mt
        return None, GeneratedImage(
            url=f"data:{mt};base64,{cb64}",
            width=width, height=height, artifact_id=None,
        )

    # Optionally produce a thumbnail variant. Best-effort: any failure logs
    # and continues with the existing variants.
    want_thumbnail = bool(return_variants and "thumbnail" in return_variants)
    if want_thumbnail:
        try:
            thumb_bytes = await asyncio.to_thread(make_thumbnail, raw_bytes)
            if thumb_bytes:
                await artifact_storage.create_artifact(
                    session_id=effective_session,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    type="image",
                    format="png",
                    title=f"Thumbnail: {prompt[:60]}",
                    filename=f"thumb_{uuid.uuid4().hex[:8]}_{index + 1}.png",
                    content=thumb_bytes,
                    source="image_generation_thumbnail",
                    variant="thumbnail",
                    parent_artifact_id=raw_artifact_id,
                    turn_id=turn_id,
                    owner_scope=owner_scope,
                    provider=provider,
                    model_id=model_id,
                    prompt=prompt,
                )
        except Exception as exc:
            logger.warning("Thumbnail persist failed: %s", exc)

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
            variant="display",
            parent_artifact_id=raw_artifact_id,
            turn_id=turn_id,
            owner_scope=owner_scope,
            width=width,
            height=height,
            provider=provider,
            model_id=model_id,
            prompt=prompt,
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


async def _persist_and_get_url_bounded(*args, **kwargs) -> tuple[str | None, GeneratedImage]:
    async with _bounded(
        _persistence_semaphore,
        status_code=503,
        error_code="persistence_busy",
        message="Image persistence concurrency is saturated",
        retry_after=3,
    ):
        return await _persist_and_get_url(*args, **kwargs)


async def _load_artifact_bytes_owner_scoped(
    artifact_storage,
    artifact_id: str,
    *,
    owner_scope: str,
    user: UserContext | None,
) -> bytes:
    """Resolve raw bytes for an artifact_id with owner-scope enforcement.

    Returns the raw variant's bytes (walking variants if necessary). Raises
    HTTPException 404 on missing / owner mismatch (same code so attackers
    can't enumerate ids by error-code timing).
    """
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail="ArtifactStorage not configured",
        )
    # Find the raw variant in this artifact's family. Scope-check both the
    # original lookup and the raw it points to, since legacy data may have
    # NULL owner_scope (in which case we fall back to tenant_id+user_id).
    try:
        raw = await artifact_storage.find_variant(artifact_id, "raw")
    except Exception as exc:
        # Storage doesn't expose ``find_variant`` (legacy / mocked storage in
        # tests) or the call blew up. Fall back to the simpler get_artifact
        # path so legacy callers and unit-test mocks keep working.
        logger.debug(
            "find_variant unavailable on artifact %s (%s) — falling back to get_artifact",
            artifact_id, exc,
        )
        try:
            raw = await artifact_storage.get_artifact(artifact_id)
        except Exception as exc2:
            logger.warning("artifact lookup failed for %s: %s", artifact_id, exc2)
            raise HTTPException(
                status_code=404,
                detail=f"artifact {artifact_id!r} not found",
            ) from exc2
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id!r} not found",
        )
    raw_owner_scope = getattr(raw, "owner_scope", None)
    if isinstance(raw_owner_scope, str) and raw_owner_scope:
        owner_matches = raw_owner_scope == owner_scope
    else:
        owner_matches = (
            user is not None
            and getattr(raw, "tenant_id", None) == user.tenant_id
            and getattr(raw, "user_id", None) == user.user_id
        )
    if not owner_matches:
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id!r} not found",
        )
    try:
        content = await artifact_storage.download_artifact(raw.artifact_id)
    except Exception as exc:
        logger.warning("artifact download failed for %s: %s", raw.artifact_id, exc)
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id!r} not found",
        ) from exc
    if not content:
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id!r} not found",
        )
    return content


async def _resolve_reference_bytes(
    body: ImageGenerationRequest,
    *,
    artifact_storage,
    user: UserContext | None = None,
    owner_scope: str | None = None,
    db_pool=None,
) -> tuple[str | None, str | None]:
    """Resolve any reference image to (base64_bytes, resolved_parent_artifact_id).

    Order (most-secure first):
    1. ``parent_artifact_id`` — explicit lineage anchor (image-redesign Phase 2).
    2. ``reference_artifact_id`` — direct lookup in our ArtifactStorage. No
       URL fetch, no SSRF surface. **Preferred** for stateless edits.
    3. ``reference_blob_id`` — object storage blob uploaded/fetched earlier.
    4. session-derived latest_artifact (when ``session_id`` set + image_session
       row exists with a non-null ``latest_artifact_id``).
    5. ``reference_image`` (base64 / data-URL) — converted to blob when
       object storage is available, then passed to Gemini inline at the last hop.
    6. ``reference_image_url`` — SSRF-safe fetch, converted to blob when
       object storage is available.

    Returns ``(None, None)`` if no reference is provided.
    """
    _validate_single_explicit_reference(body)

    # 1. parent_artifact_id (explicit anchor)
    if body.parent_artifact_id:
        content = await _load_artifact_bytes_owner_scoped(
            artifact_storage,
            body.parent_artifact_id,
            owner_scope=owner_scope or (user.user_id if user else ""),
            user=user,
        )
        if len(content) > _REFERENCE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"error_code": "reference_too_large", "message": "reference artifact exceeds size limit"},
            )
        return _b64.b64encode(content).decode(), body.parent_artifact_id

    # 2. reference_artifact_id (legacy)
    if body.reference_artifact_id:
        content = await _load_artifact_bytes_owner_scoped(
            artifact_storage,
            body.reference_artifact_id,
            owner_scope=owner_scope or (user.user_id if user else ""),
            user=user,
        )
        if len(content) > _REFERENCE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"error_code": "reference_too_large", "message": "reference artifact exceeds size limit"},
            )
        return _b64.b64encode(content).decode(), body.reference_artifact_id

    # 3. reference_blob_id
    if body.reference_blob_id:
        content = await _download_blob_bytes(
            pool=db_pool,
            artifact_storage=artifact_storage,
            blob_id=body.reference_blob_id,
            owner_scope=owner_scope or (user.user_id if user else ""),
        )
        return _b64.b64encode(content).decode(), None

    # 4. session-derived latest
    if body.session_id and db_pool is not None and owner_scope is not None:
        try:
            row = await get_image_session(db_pool, body.session_id)
        except Exception as exc:
            logger.warning("image_session lookup failed: %s", exc)
            row = None
        if row and row.get("latest_artifact_id"):
            latest_id = row["latest_artifact_id"]
            content = await _load_artifact_bytes_owner_scoped(
                artifact_storage, latest_id,
                owner_scope=owner_scope, user=user,
            )
            if len(content) > _REFERENCE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={"error_code": "reference_too_large", "message": "reference artifact exceeds size limit"},
                )
            return _b64.b64encode(content).decode(), latest_id

    # 5. raw base64 / data URL
    if body.reference_image:
        ref_b64, content, mime_type = _normalize_reference_image_b64(body.reference_image)
        if artifact_storage and db_pool is not None and owner_scope and user:
            try:
                await _store_blob_bytes(
                    pool=db_pool,
                    artifact_storage=artifact_storage,
                    owner_scope=owner_scope,
                    user=user,
                    content=content,
                    mime_type=mime_type,
                    source="inline_request",
                    filename="reference.png",
                )
            except HTTPException:
                if _requires_persistent_task_store():
                    raise
            except Exception as exc:
                if _requires_persistent_task_store():
                    raise
                logger.debug("reference_image blob persist skipped: %s", exc)
        return ref_b64, None

    # 6. reference_image_url
    if not body.reference_image_url:
        return None, None

    try:
        content = await safe_fetch(
            body.reference_image_url,
            max_bytes=8 * 1024 * 1024,
            max_redirects=3,
            timeout=30.0,
        )
        if len(content) > _REFERENCE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"error_code": "reference_too_large", "message": "reference image exceeds size limit"},
            )
        if artifact_storage and db_pool is not None and owner_scope and user:
            try:
                await _store_blob_bytes(
                    pool=db_pool,
                    artifact_storage=artifact_storage,
                    owner_scope=owner_scope,
                    user=user,
                    content=content,
                    mime_type="image/png",
                    source="fetched_url",
                    filename="reference.png",
                )
            except HTTPException:
                if _requires_persistent_task_store():
                    raise
            except Exception as exc:
                if _requires_persistent_task_store():
                    raise
                logger.debug("reference_image_url blob persist skipped: %s", exc)
        return _b64.b64encode(content).decode(), None
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


def _limit_replay_history(image_history: list[dict]) -> list[dict]:
    """Keep only the most recent visual model turns and their prompts.

    Gemini replay needs image bytes inline at the final provider hop. Limiting
    visual turns bounds S3 reads, base64 expansion, and request size while
    preserving the latest edit context.
    """
    if _REPLAY_MAX_VISUAL_TURNS <= 0:
        return []
    visual_seen = 0
    keep_reversed: list[dict] = []
    for turn in reversed(image_history):
        is_visual_model = (
            turn.get("role") == "model"
            and (turn.get("artifact_id") or turn.get("image_base64") or turn.get("file_uri"))
        )
        if is_visual_model:
            if visual_seen >= _REPLAY_MAX_VISUAL_TURNS:
                continue
            visual_seen += 1
        keep_reversed.append(turn)
    return list(reversed(keep_reversed))


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

    bounded_history = _limit_replay_history(image_history)
    resolved_history = await inflate_history_with_bytes(bounded_history, _download)
    contents = build_gemini_contents_from_history(resolved_history, styled_prompt)

    async with _bounded(
        _provider_semaphore,
        status_code=429,
        error_code="provider_busy",
        message="Gemini image provider concurrency is saturated",
    ):
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
    owner_scope: str | None = None,
    turn_id: str | None = None,
    provider: str | None = "google",
    model_id: str | None = None,
    return_variants: list[str] | None = None,
    write_legacy_metadata: bool = False,
) -> tuple[str | None, list[GeneratedImage]]:
    """Store ALL newly-generated images as artifacts (concurrently), append a
    single canonical turn to the DB state. Returns
    ``(canonical_artifact_id, [GeneratedImage, ...])`` — the canonical id is
    the first image's id, used as the visual anchor for the next-turn replay.

    New turns are not written to legacy ``image_chat_history`` metadata. The
    route still reads legacy metadata for migration-time replay, but
    ``image_turns`` + ``image_sessions.latest_artifact_id`` are the authority
    for new state.
    """
    session, image_history, effective_preset = session_state
    _cap_result_images(res, body.n)

    persisted = await asyncio.gather(*[
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
            provider=provider,
            model_id=model_id,
            return_variants=return_variants,
        )
        for i, img in enumerate(res.images)
    ])
    canonical_artifact_id = persisted[0][0]
    generated_list = [gi for _, gi in persisted]

    if write_legacy_metadata and session_manager and session:
        append_image_turns(
            image_history, body.prompt, res.images[0], res.text,
            artifact_id=canonical_artifact_id,
        )
        meta = dict(session.metadata or {})
        meta["image_chat_history"] = image_history
        meta["style_preset"] = effective_preset.value
        try:
            await session_manager.update_metadata(body.session_id, meta)
        except Exception as exc:
            logger.warning("Legacy image_chat_history write failed for %s: %s", body.session_id, exc)

    return canonical_artifact_id, generated_list


# -----------------------------------------------------------------------------
# Image-redesign pipeline helpers — owner_scope, idempotency, CAS lineage
# -----------------------------------------------------------------------------


def _get_db_pool(request: Request):
    """Reach the assistant-service's asyncpg pool (or None in dev/tests).

    The session_manager owns the pool — it's the same DB we use for
    session/metadata writes. ``database._pool`` is the asyncpg pool
    handle exposed by ``DatabaseStorage`` (Phase 5f Batch C).
    """
    smgr = getattr(request.app.state, "session_manager", None)
    db = getattr(smgr, "database", None)
    pool = getattr(db, "_pool", None)
    if type(pool).__module__.startswith("unittest.mock"):
        return None
    return pool


def _request_payload_for_hash(body: ImageGenerationRequest) -> dict[str, Any]:
    """Stable dict used for idempotency request_hash computation."""
    return {
        "prompt": body.prompt,
        "model_id": body.model_id,
        "n": body.n,
        "size": body.size,
        "style": getattr(body.style, "value", str(body.style)),
        "session_id": body.session_id,
        "reference_artifact_id": body.reference_artifact_id,
        "reference_blob_id": body.reference_blob_id,
        "reference_image_url": body.reference_image_url,
        "reference_image_present": bool(body.reference_image),  # don't hash bytes
        "add_watermark": body.add_watermark,
        "app_user_id": body.app_user_id,
        "app_tenant_id": body.app_tenant_id,
        "parent_artifact_id": body.parent_artifact_id,
        "expected_parent_artifact_id": body.expected_parent_artifact_id,
        "return_variants": sorted(body.return_variants) if body.return_variants else None,
        "allow_branch": body.allow_branch,
    }


def _resolve_owner_scope(user: UserContext, body: ImageGenerationRequest) -> str:
    """Compute the owner_scope for a request from headers + body."""
    return _compute_owner_scope(
        user.user_id,
        app_tenant_id=body.app_tenant_id or user.app_tenant_id,
        app_user_id=body.app_user_id or user.app_user_id,
    )


def _resolve_owner_scope_from_user(user: UserContext) -> str:
    """Compute owner_scope for read-only routes that do not carry a body."""
    return _compute_owner_scope(
        user.user_id,
        app_tenant_id=user.app_tenant_id,
        app_user_id=user.app_user_id,
    )


def _task_is_visible_to_user(task: dict[str, Any], user: UserContext) -> bool:
    """Return True when task ownership can be proven for this caller."""
    task_owner_scope = task.get("owner_scope")
    if isinstance(task_owner_scope, str) and task_owner_scope:
        return task_owner_scope == _resolve_owner_scope_from_user(user)

    owner_tenant_id = task.get("owner_tenant_id")
    owner_user_id = task.get("owner_user_id")
    if owner_tenant_id or owner_user_id:
        return owner_tenant_id == user.tenant_id and owner_user_id == user.user_id

    return False


async def _check_idempotency(
    pool,
    *,
    owner_scope: str,
    body: ImageGenerationRequest,
) -> tuple[str | None, bool, str | None]:
    """Look-only idempotency probe (no claim).

    Used by the sync route at request entry to detect already-recorded
    keys. Async path uses ``record_idempotent`` directly so the claim
    races atomically against concurrent submits.

    Returns ``(replay_task_id, conflict, request_hash)`` where:
      * ``replay_task_id`` set when an existing same-hash row is found
      * ``conflict`` True when client_request_id exists with a different
        body hash → 409 idempotency_conflict
      * ``request_hash`` always returned when client_request_id is set
    """
    if not body.client_request_id or pool is None:
        return None, False, None
    request_hash = _compute_request_hash(_request_payload_for_hash(body))
    existing = await lookup_idempotent(
        pool, owner_scope=owner_scope, client_request_id=body.client_request_id,
    )
    if existing:
        if existing["request_hash"] != request_hash:
            return None, True, request_hash
        return existing["task_id"], False, request_hash
    return None, False, request_hash


async def _ensure_image_session(
    pool,
    *,
    session_id: str,
    owner_scope: str,
    user: UserContext,
    body: ImageGenerationRequest,
) -> dict | None:
    """Ensure ``image_sessions`` row exists for (session_id, owner_scope).

    Returns the row (post-upsert). Owner-scope mismatch on existing row →
    raises 404 (treat as nonexistent for the new owner).
    """
    if pool is None:
        return None
    existing = await get_image_session(pool, session_id)
    if existing and existing.get("owner_scope") and existing.get("owner_scope") != owner_scope:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found",
                    "message": f"image session {session_id!r} not found"},
        )
    await upsert_image_session(
        pool,
        session_id=session_id,
        owner_scope=owner_scope,
        app_user_id=body.app_user_id or user.app_user_id,
        app_tenant_id=body.app_tenant_id or user.app_tenant_id,
        locked_style=None,  # don't touch on upsert
    )
    return await get_image_session(pool, session_id)


async def _check_expected_parent(
    pool,
    *,
    session_id: str,
    expected_parent: str | None,
) -> None:
    """If caller provided ``expected_parent_artifact_id``, verify it matches
    the session's current ``latest_artifact_id``. Raises 409 on mismatch."""
    if not expected_parent or pool is None:
        return
    row = await get_image_session(pool, session_id)
    current = row.get("latest_artifact_id") if row else None
    if current != expected_parent:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "latest_artifact_conflict",
                "message": "expected_parent_artifact_id does not match current session latest",
                "current_latest_artifact_id": current,
            },
        )


async def _resolve_style_for_session(
    pool,
    *,
    session_id: str | None,
    body_style: StylePreset,
    style_explicit: bool,
) -> tuple[StylePreset, str | None, bool]:
    """Apply image_sessions.locked_style state machine.

    Inputs:
      * ``style_explicit`` — caller actually set ``style`` in the request
        body. Distinguishes "I'm leaving style alone" (False → inherit lock)
        from "I explicitly want default" (True + DEFAULT → clear lock).

    Rules:
      1. style_explicit=False, has locked  → use locked, no write.
      2. style_explicit=False, no locked   → DEFAULT, no write.
      3. style_explicit=True,  non-DEFAULT → use given, write as new lock.
      4. style_explicit=True,  DEFAULT     → clear lock, return DEFAULT.

    Returns ``(effective_preset, new_locked_style_or_None, clear_lock)``.
    Caller should:
      * write ``new_locked_style`` if not None
      * call set_locked_style(None) if ``clear_lock`` is True.
    """
    if not session_id or pool is None:
        return body_style, None, False
    row = await get_image_session(pool, session_id)
    locked = row.get("locked_style") if row else None
    locked_preset = resolve_style_preset(locked) if locked else None

    if style_explicit and body_style is not StylePreset.DEFAULT:
        return body_style, body_style.value, False
    if style_explicit and body_style is StylePreset.DEFAULT:
        # Explicit reset to default clears the lock.
        return StylePreset.DEFAULT, None, True
    # Not explicit — inherit if available.
    if locked_preset is not None:
        return locked_preset, None, False
    return StylePreset.DEFAULT, None, False


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
    """Persist a row into image_turns. session_id-less stateless turns get
    a synthetic session id so the row's NOT NULL constraint holds, but they
    won't show up in any /image-sessions/{id} listing."""
    if pool is None:
        return
    effective_session = session_id or f"stateless_{turn_id}"
    await insert_turn(
        pool,
        turn_id=turn_id,
        session_id=effective_session,
        owner_scope=owner_scope,
        task_id=task_id,
        prompt=body.prompt,
        model_id=body.model_id,
        style=getattr(body.style, "value", None),
        add_watermark=body.add_watermark,
        parent_artifact_id=parent_artifact_id,
        output_artifact_id=output_artifact_id,
        status=status,
        error=error,
        error_code=error_code,
        client_request_id=body.client_request_id,
        request_hash=request_hash,
        completed_at=datetime.now(timezone.utc) if status in ("completed", "failed") else None,
        thought_signature=thought_signature,
        provider_text=provider_text,
        output_artifact_ids=output_artifact_ids,
        state=state or status,
    )


async def _build_variants_response(
    artifact_storage,
    *,
    raw_artifact_id: str | None,
    return_variants: list[str] | None,
    owner_scope: str,
) -> dict[str, str] | None:
    """Build the optional variants response map.

    Skips variants that don't resolve. Owner-scope is enforced via the
    presigned-url helper.
    """
    if not return_variants or not raw_artifact_id or not artifact_storage:
        return None
    out: dict[str, str] = {}
    for v in return_variants:
        if v not in ("raw", "display", "thumbnail"):
            continue
        try:
            url, actual = await artifact_storage.get_presigned_download_url_for_variant(
                raw_artifact_id, v, owner_scope=owner_scope,
            )
        except Exception as exc:
            logger.warning("variant resolve failed for %s/%s: %s", raw_artifact_id, v, exc)
            continue
        if url and actual:
            out[v] = url
    return out or None


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
            detail={"error_code": "db_unavailable", "message": "Image blob database is unavailable"},
        )
    artifact_storage = _get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "storage_unavailable", "message": "ArtifactStorage not configured"},
        )
    mime_type = _validate_image_mime(body.mime_type)
    if body.byte_size is not None and body.byte_size > _REFERENCE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error_code": "reference_too_large", "message": "image blob exceeds size limit"},
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
            detail={"error_code": "presigned_upload_unavailable", "message": "Storage backend does not support presigned uploads"},
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
            detail={"error_code": "db_unavailable", "message": "Image blob database is unavailable"},
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
            detail={"error_code": "reference_too_large", "message": "image blob exceeds size limit"},
        )
    artifact_storage = _get_artifact_storage()
    backend = _get_storage_backend(artifact_storage)
    exists = await backend.exists(row["storage_key"])
    if not exists:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "upload_missing", "message": "uploaded object was not found in storage"},
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
            detail={"error_code": "db_unavailable", "message": "Image blob database is unavailable"},
        )
    artifact_storage = _get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "storage_unavailable", "message": "ArtifactStorage not configured"},
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


def _task_to_sync_response(task: dict, *, idempotent_replay: bool = False) -> ImageGenerationResponse:
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
        except Exception:
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
            pool, owner_scope=owner_scope, body=body,
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
                pool, owner_scope=owner_scope,
                client_request_id=body.client_request_id,
                request_hash=request_hash, task_id=turn_id,
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
            body.model_id, selected_provider,
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
        if (
            body.session_id and pool is not None
            and not has_explicit_ref
        ):
            sess_row = await get_image_session(pool, body.session_id)
            session_implies_reference = bool(
                sess_row and sess_row.get("latest_artifact_id")
            )
        has_reference = has_explicit_ref or session_implies_reference

        if has_reference and not prefer_gemini:
            return ImageGenerationResponse(
                success=False, images=[], provider=str(selected_provider or "unknown"),
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
                pool, session_id=body.session_id, owner_scope=owner_scope,
                user=user, body=body,
            )
            await _check_expected_parent(
                pool, session_id=body.session_id,
                expected_parent=body.expected_parent_artifact_id,
            )

        # ---- Style lock ------------------------------------------------
        style_explicit = "style" in body.model_fields_set
        effective_style, new_locked_style, clear_lock = await _resolve_style_for_session(
            pool, session_id=body.session_id, body_style=body.style,
            style_explicit=style_explicit,
        )

        width, height, aspect_ratio = parse_image_size(body.size)

        # ---- Resolve reference bytes ----------------------------------
        ref_b64: str | None = None
        resolved_parent: str | None = None
        if prefer_gemini and (has_explicit_ref or session_implies_reference):
            try:
                ref_b64, resolved_parent = await _resolve_reference_bytes(
                    body, artifact_storage=artifact_storage, user=user,
                    owner_scope=owner_scope, db_pool=pool,
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    await _record_turn(
                        pool, turn_id=turn_id, session_id=body.session_id,
                        owner_scope=owner_scope, task_id=None, body=body,
                        parent_artifact_id=None, output_artifact_id=None,
                        status="failed", error="reference artifact not found",
                        error_code="reference_not_found", request_hash=request_hash,
                    )
                raise

        # ---- Provider call --------------------------------------------
        styled_prompt = compose_styled_prompt(body.prompt, effective_style)
        provider_label: str | None = None

        if prefer_gemini and ref_b64 is not None:
            gemini = get_gemini_image_generator()
            if not gemini.is_configured:
                return ImageGenerationResponse(
                    success=False, images=[], provider="none",
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
                    prompt=styled_prompt, n=body.n,
                    aspect_ratio=aspect_ratio, reference_image=ref_b64,
                )
            provider_label = "google"
        elif body.session_id and session_mgr and prefer_gemini:
            # No reference resolved but session+gemini → fall back to legacy
            # multi-turn path (history-backed). New behavior: when caller
            # has explicit parent_artifact_id we already resolved bytes above,
            # so this branch only fires for "session_id alone, never edited".
            res, session_state, err = await _run_gemini_multi_turn(
                body, aspect_ratio=aspect_ratio, width=width, height=height,
                session_manager=session_mgr, user=user,
                artifact_storage=artifact_storage,
            )
            if err:
                return ImageGenerationResponse(
                    success=False, images=[], provider="none",
                    duration_ms=(time.time() - start_time) * 1000, error=err,
                    error_code="provider_unavailable",
                    session_id=body.session_id,
                    client_request_id=body.client_request_id,
                )
            if not (res and res.success and res.images):
                err_msg = (res.error if res else None) or "Image generation failed"
                return ImageGenerationResponse(
                    success=False, images=[], provider="google",
                    duration_ms=((res.duration_ms if res else None)
                                 or (time.time() - start_time) * 1000),
                    error=err_msg,
                    error_code="provider_failed",
                    session_id=body.session_id,
                    client_request_id=body.client_request_id,
                )
            # Persist via the legacy multi-turn helper (writes session metadata)
            raw_anchor, generated_list = await _persist_multi_turn_result(
                body, res=res, session_state=session_state,
                session_manager=session_mgr, user=user,
                artifact_storage=artifact_storage,
                width=width, height=height,
                owner_scope=owner_scope,
                turn_id=turn_id,
                provider="google",
                model_id=body.model_id,
                return_variants=body.return_variants,
                write_legacy_metadata=pool is None,
            )
            latest_advanced = await _post_generation_bookkeeping(
                pool, artifact_storage=artifact_storage,
                turn_id=turn_id, session_id=body.session_id,
                owner_scope=owner_scope, body=body,
                resolved_parent=resolved_parent,
                raw_anchor=raw_anchor, request_hash=request_hash,
                new_locked_style=new_locked_style,
                clear_lock=clear_lock,
            )
            return ImageGenerationResponse(
                success=True, images=generated_list,
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
                    prompt=styled_prompt, n=body.n, size=body.size or "1536*1536",
                    style=dashscope_tag, negative_prompt=negative_prompt,
                    aspect_ratio=aspect_ratio,
                    prefer_gemini=prefer_gemini, prefer_doubao=prefer_doubao,
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
                pool, turn_id=turn_id, session_id=body.session_id,
                owner_scope=owner_scope, task_id=None, body=body,
                parent_artifact_id=resolved_parent, output_artifact_id=None,
                status="failed", error=err, error_code=error_code,
                request_hash=request_hash,
            )
            return ImageGenerationResponse(
                success=False, images=[], provider=provider_label,
                duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
                error=err, error_code=error_code,
                session_id=body.session_id,
                client_request_id=body.client_request_id,
            )

        # ---- Persist artifacts ----------------------------------------
        persisted = await asyncio.gather(*[
            _persist_and_get_url_bounded(
                img, artifact_storage=artifact_storage,
                session_id=body.session_id, user=user, prompt=body.prompt,
                add_watermark=body.add_watermark, width=width, height=height,
                index=i,
                owner_scope=owner_scope, turn_id=turn_id,
                parent_artifact_id=resolved_parent,
                provider=provider_label, model_id=body.model_id,
                return_variants=body.return_variants,
            )
            for i, img in enumerate(res.images)
        ])
        raw_anchor = persisted[0][0] if persisted else None
        generated_list = [gi for _, gi in persisted]

        # ---- CAS advance + style lock + turn audit + idempotency claim
        latest_advanced = await _post_generation_bookkeeping(
            pool, artifact_storage=artifact_storage,
            turn_id=turn_id, session_id=body.session_id,
            owner_scope=owner_scope, body=body,
            resolved_parent=resolved_parent,
            raw_anchor=raw_anchor, request_hash=request_hash,
            new_locked_style=new_locked_style,
            clear_lock=clear_lock,
        )

        return ImageGenerationResponse(
            success=True, images=generated_list, provider=provider_label,
            duration_ms=res.duration_ms or (time.time() - start_time) * 1000,
            session_id=body.session_id,
            turn_id=turn_id,
            parent_artifact_id=resolved_parent,
            output_artifact_id=raw_anchor,
            client_request_id=body.client_request_id,
            idempotent_replay=False,
            latest_advanced=latest_advanced,
            variants=await _build_variants_response(
                artifact_storage, raw_artifact_id=raw_anchor,
                return_variants=body.return_variants, owner_scope=owner_scope,
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Image generation failed: %s", e)
        # Best-effort failure-record so callers can audit via /image-sessions
        with suppress(Exception):
            await _record_turn(
                pool, turn_id=turn_id, session_id=body.session_id,
                owner_scope=owner_scope, task_id=None, body=body,
                parent_artifact_id=None, output_artifact_id=None,
                status="failed", error=str(e), error_code="internal_error",
                request_hash=None,
            )
        return ImageGenerationResponse(
            success=False, images=[], provider="unknown",
            duration_ms=(time.time() - start_time) * 1000, error=str(e),
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
    """Centralized post-success state writes:

    * CAS-advance latest_artifact_id (skipped when allow_branch=True or no session)
    * Set locked_style if the caller pinned a new one (or clear it on explicit reset)
    * Insert image_turns row with status=completed
    * Claim idempotency record (if client_request_id set)
    """
    # Style lock write
    if session_id and pool is not None:
        if new_locked_style:
            try:
                await _set_locked_style(pool, session_id, new_locked_style)
            except Exception as exc:
                logger.warning("set_locked_style failed: %s", exc)
        elif clear_lock:
            try:
                await _set_locked_style(pool, session_id, None)
            except Exception as exc:
                logger.warning("clear_locked_style failed: %s", exc)

    # CAS advance
    latest_advanced = True  # default for no-session / allow_branch cases
    if session_id and raw_anchor and pool is not None and not body.allow_branch:
        try:
            latest_advanced = await advance_latest_artifact_cas(
                pool, session_id=session_id,
                expected_parent=resolved_parent,
                new_artifact_id=raw_anchor,
            )
            if not latest_advanced:
                logger.info(
                    "latest_artifact CAS lost race session=%s parent=%s — output is a branch",
                    session_id, resolved_parent,
                )
        except Exception as exc:
            logger.warning("advance_latest_artifact_cas failed: %s", exc)
            latest_advanced = False
    elif session_id and body.allow_branch:
        # Caller explicitly asked for a branch — don't advance, don't claim.
        latest_advanced = False

    # Turn audit
    try:
        await _record_turn(
            pool, turn_id=turn_id, session_id=session_id,
            owner_scope=owner_scope, task_id=None, body=body,
            parent_artifact_id=resolved_parent, output_artifact_id=raw_anchor,
            status="completed", error=None, error_code=None,
            request_hash=request_hash,
            output_artifact_ids=[raw_anchor] if raw_anchor else None,
            state="completed",
        )
    except Exception as exc:
        logger.warning("record_turn failed: %s", exc)

    # NOTE: idempotency claim was made BEFORE generation in the route
    # handlers (sync + async paths). Bookkeeping no longer claims —
    # otherwise a worker crash mid-generation would race with the route
    # claim. Single source of truth = pre-work claim.

    return latest_advanced



# -----------------------------------------------------------------------------
# POST /generate-image-async + GET /image-task/{task_id}
# -----------------------------------------------------------------------------


async def _run_image_generation_task(
    task_id: str, body: AsyncImageGenerationRequest,
    model_registry: ModelRegistry, user: UserContext,
    session_manager=None, redis=None, pool=None,
) -> None:
    task = await _load_task(redis, task_id)
    if task is None:
        # Defensive: caller should have stored the task before scheduling us.
        logger.error("Async image task %s vanished before worker started", task_id)
        return
    task["status"] = "running"
    task["progress"] = 10
    await _store_task(redis, task_id, task, pool=pool)
    start_time = time.time()

    owner_scope = task.get("owner_scope") or _resolve_owner_scope(user, body)
    turn_id = task.get("turn_id") or new_turn_id()
    request_hash = task.get("request_hash")
    resolved_parent: str | None = None
    raw_anchor: str | None = None

    # Mark turn as running for parallel observers
    with suppress(Exception):
        await update_turn_status(pool, turn_id=turn_id, status="running")

    try:
        model_info = model_registry.get_model(body.model_id) if model_registry else None
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id, selected_provider,
        )
        width, height, aspect_ratio = parse_image_size(body.size)
        task["progress"] = 30

        artifact_storage = _get_artifact_storage()
        has_explicit_ref = bool(
            body.parent_artifact_id
            or body.reference_artifact_id
            or body.reference_blob_id
            or body.reference_image
            or body.reference_image_url
        )
        session_implies_reference = False
        if (
            body.session_id and pool is not None and not has_explicit_ref
        ):
            sess_row = await get_image_session(pool, body.session_id)
            session_implies_reference = bool(
                sess_row and sess_row.get("latest_artifact_id")
            )
        has_reference = has_explicit_ref or session_implies_reference

        if has_reference and not prefer_gemini:
            task["status"] = "failed"
            task["error"] = (
                f"reference image editing requires a Gemini model "
                f"(got model_id={body.model_id!r}, provider={selected_provider!r})"
            )
            task["error_code"] = "reference_requires_gemini"
            task["progress"] = 100
            task["completed_at"] = datetime.now(timezone.utc).isoformat()
            await _store_task(redis, task_id, task, pool=pool)
            await update_turn_status(
                pool, turn_id=turn_id, status="failed",
                error=task["error"], error_code="reference_requires_gemini",
            )
            if body.callback_url:
                try:
                    await send_image_callback(body.callback_url, task)
                except Exception as e:
                    logger.warning("Callback to %s failed: %s", body.callback_url, e)
            return

        # Style lock resolution
        style_explicit = "style" in body.model_fields_set
        effective_style, new_locked_style, clear_lock = await _resolve_style_for_session(
            pool, session_id=body.session_id, body_style=body.style,
            style_explicit=style_explicit,
        )

        # Resolve reference bytes (may raise 404 → recorded as failed turn)
        ref_b64: str | None = None
        if prefer_gemini and has_reference:
            try:
                ref_b64, resolved_parent = await _resolve_reference_bytes(
                    body, artifact_storage=artifact_storage, user=user,
                    owner_scope=owner_scope, db_pool=pool,
                )
            except HTTPException as exc:
                msg = exc.detail if isinstance(exc.detail, str) else "reference not found"
                task["status"] = "failed"
                task["error"] = msg
                task["error_code"] = "reference_not_found"
                task["http_status_code"] = exc.status_code
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await _store_task(redis, task_id, task, pool=pool)
                await update_turn_status(
                    pool, turn_id=turn_id, status="failed",
                    error=msg, error_code="reference_not_found",
                )
                if body.callback_url:
                    with suppress(Exception):
                        await send_image_callback(body.callback_url, task)
                return

        res = None
        generated_response_imgs: list[GeneratedImage] = []
        provider_label: str | None = None
        styled_prompt = compose_styled_prompt(body.prompt, effective_style)

        if prefer_gemini and ref_b64 is not None:
            gemini = get_gemini_image_generator()
            if not gemini.is_configured:
                task["status"] = "failed"
                task["error"] = "Gemini API key not configured"
                task["error_code"] = "provider_unavailable"
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await _store_task(redis, task_id, task, pool=pool)
                await update_turn_status(
                    pool, turn_id=turn_id, status="failed",
                    error=task["error"], error_code="provider_unavailable",
                )
                if body.callback_url:
                    with suppress(Exception):
                        await send_image_callback(body.callback_url, task)
                return
            async with _bounded(
                _provider_semaphore,
                status_code=429,
                error_code="provider_busy",
                message="Gemini image provider concurrency is saturated",
            ):
                res = await gemini.generate(
                    prompt=styled_prompt, n=body.n, aspect_ratio=aspect_ratio,
                    reference_image=ref_b64,
                )
            provider_label = "google"

        if res is None and body.session_id and session_manager and prefer_gemini:
            # Legacy session-history backed flow (no explicit parent)
            res, session_state, err = await _run_gemini_multi_turn(
                body, aspect_ratio=aspect_ratio, width=width, height=height,
                session_manager=session_manager, user=user,
                artifact_storage=artifact_storage,
            )
            if err:
                task["status"] = "failed"
                task["error"] = err
                task["error_code"] = "provider_unavailable"
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await _store_task(redis, task_id, task, pool=pool)
                await update_turn_status(
                    pool, turn_id=turn_id, status="failed",
                    error=err, error_code="provider_unavailable",
                )
                if body.callback_url:
                    with suppress(Exception):
                        await send_image_callback(body.callback_url, task)
                return
            if res and res.success and res.images:
                raw_anchor, generated_list = await _persist_multi_turn_result(
                    body, res=res, session_state=session_state,
                    session_manager=session_manager, user=user,
                    artifact_storage=artifact_storage,
                    width=width, height=height,
                    owner_scope=owner_scope,
                    turn_id=turn_id,
                    provider="google",
                    model_id=body.model_id,
                    return_variants=body.return_variants,
                    write_legacy_metadata=pool is None,
                )
                generated_response_imgs.extend(generated_list)
                provider_label = "google"

        if res is None:
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
                    prompt=styled_prompt, n=body.n, size=body.size or "1536*1536",
                    style=dashscope_tag, negative_prompt=negative_prompt,
                    aspect_ratio=aspect_ratio,
                    prefer_gemini=prefer_gemini, prefer_doubao=prefer_doubao,
                    dashscope_model=dashscope_model,
                )
            provider_label = res.provider

        _cap_result_images(res, body.n)
        duration_ms = (time.time() - start_time) * 1000
        task["duration_ms"] = duration_ms
        task["provider"] = provider_label

        if not res.success:
            err = res.error or "Image generation failed"
            error_code = "provider_failed"
            if res.blocked and res.block_reason:
                err = f"{err} (blocked: {res.block_reason})"
                error_code = "provider_blocked"
            task["status"] = "failed"
            task["error"] = err
            task["error_code"] = error_code
            task["progress"] = 100
            task["completed_at"] = datetime.now(timezone.utc).isoformat()
            await _store_task(redis, task_id, task, pool=pool)
            await update_turn_status(
                pool, turn_id=turn_id, status="failed",
                error=err, error_code=error_code,
            )
            if body.callback_url:
                with suppress(Exception):
                    await send_image_callback(body.callback_url, task)
            return

        task["progress"] = 70

        if not generated_response_imgs:
            persisted = await asyncio.gather(*[
                _persist_and_get_url_bounded(
                    img, artifact_storage=artifact_storage,
                    session_id=body.session_id, user=user, prompt=body.prompt,
                    add_watermark=body.add_watermark, width=width, height=height,
                    index=i,
                    owner_scope=owner_scope, turn_id=turn_id,
                    parent_artifact_id=resolved_parent,
                    provider=provider_label, model_id=body.model_id,
                    return_variants=body.return_variants,
                )
                for i, img in enumerate(res.images)
            ])
            generated_response_imgs = [gi for _, gi in persisted]
            raw_anchor = persisted[0][0] if persisted else None

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
        task["parent_artifact_id"] = resolved_parent
        task["output_artifact_id"] = raw_anchor
        task["turn_id"] = turn_id
        task["session_id"] = body.session_id
        task["client_request_id"] = body.client_request_id

        # Post-success bookkeeping (CAS, locked_style, turn audit)
        latest_advanced = await _post_generation_bookkeeping(
            pool, artifact_storage=artifact_storage,
            turn_id=turn_id, session_id=body.session_id,
            owner_scope=owner_scope, body=body,
            resolved_parent=resolved_parent, raw_anchor=raw_anchor,
            request_hash=request_hash, new_locked_style=new_locked_style,
            clear_lock=clear_lock,
        )
        task["latest_advanced"] = latest_advanced

    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        logger.warning("Async image generation task %s rejected: %s", task_id, detail)
        task["status"] = "failed"
        task["error"] = detail.get("message") or str(e.detail)
        task["error_code"] = detail.get("error_code") or "request_failed"
        task["http_status_code"] = e.status_code
        task["progress"] = 100
        task["duration_ms"] = (time.time() - start_time) * 1000
        task["completed_at"] = datetime.now(timezone.utc).isoformat()
        with suppress(Exception):
            await update_turn_status(
                pool, turn_id=turn_id, status="failed",
                error=task["error"], error_code=task["error_code"],
            )
    except Exception as e:
        logger.exception("Async image generation task %s failed", task_id)
        task["status"] = "failed"
        task["error"] = str(e)
        task["error_code"] = "internal_error"
        task["progress"] = 100
        task["duration_ms"] = (time.time() - start_time) * 1000
        task["completed_at"] = datetime.now(timezone.utc).isoformat()
        with suppress(Exception):
            await update_turn_status(
                pool, turn_id=turn_id, status="failed",
                error=str(e), error_code="internal_error",
            )

    await _store_task(redis, task_id, task, pool=pool)

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
        pool, owner_scope=owner_scope, body=body,
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
            task_id=replay_task_id, status=(task or {}).get("status", "pending"),
            message="Idempotent replay — existing task returned",
        )

    # Image-session bootstrap + expected_parent CAS pre-check
    if body.session_id:
        await _ensure_image_session(
            pool, session_id=body.session_id, owner_scope=owner_scope,
            user=user, body=body,
        )
        await _check_expected_parent(
            pool, session_id=body.session_id,
            expected_parent=body.expected_parent_artifact_id,
        )

    task_id = str(uuid.uuid4())
    turn_id = new_turn_id()
    now = datetime.now(timezone.utc).isoformat()
    task = {
        "task_id": task_id, "status": "pending", "progress": 0,
        "prompt": body.prompt, "model_id": body.model_id, "provider": None,
        "images": [], "duration_ms": None, "error": None,
        "error_code": None,
        "created_at": now, "completed_at": None,
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
            pool, owner_scope=owner_scope,
            client_request_id=body.client_request_id,
            request_hash=request_hash, task_id=task_id,
        )
        if not claimed:
            existing = await lookup_idempotent(
                pool, owner_scope=owner_scope,
                client_request_id=body.client_request_id,
            )
            if existing and existing["request_hash"] == request_hash:
                task = await _load_task_any(
                    getattr(request.app.state, "redis", None), pool, existing["task_id"],
                )
                return AsyncImageTaskSubmitResponse(
                    task_id=existing["task_id"], status=(task or {}).get("status", "pending"),
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
        logger.info("insert_turn succeeded: turn_id=%s task_id=%s session_id=%s",
                     turn_id, task_id, body.session_id)
    except Exception as exc:
        logger.warning("insert pending turn failed (task_id=%s): %s", task_id, exc)

    session_mgr = get_session_manager(request)
    worker = asyncio.create_task(
        _run_image_generation_task(
            task_id, body, model_registry, user,
            session_manager=session_mgr, redis=redis, pool=pool,
        )
    )
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
                                url, actual = await artifact_storage.get_presigned_download_url_for_variant(
                                    output_id, "display",
                                    owner_scope=turn.get("owner_scope"),
                                )
                            except Exception:
                                url, actual = None, None
                            if url:
                                public_id = output_id
                                if actual and actual != "raw":
                                    try:
                                        public_artifact = await artifact_storage.find_variant(output_id, actual)
                                        public_id = getattr(public_artifact, "artifact_id", output_id)
                                    except Exception:
                                        public_id = output_id
                                images = [AsyncImageArtifact(
                                    artifact_id=public_id,
                                    download_url=url,
                                    url=url,
                                )]
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
        task_id=task["task_id"], status=task["status"],
        progress=task.get("progress", 0),
        prompt=task["prompt"], model_id=task["model_id"],
        provider=task.get("provider"), images=images,
        duration_ms=task.get("duration_ms"), error=task.get("error"),
        error_code=task.get("error_code"),
        created_at=task["created_at"], completed_at=task.get("completed_at"),
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


class ArtifactDownloadUrlResponse(BaseModel):
    artifact_id: str
    variant: str = Field(
        ...,
        description=(
            "Variant actually returned. May differ from request when fallback "
            "kicks in (e.g. requested 'thumbnail' but only 'display' / 'raw' "
            "exist)."
        ),
    )
    url: str
    expires_at: str
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None


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
            detail={"error_code": "validation_error",
                    "message": "variant must be one of raw|display|thumbnail"},
        )

    artifact_storage = _get_artifact_storage()
    if not artifact_storage:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "storage_unavailable",
                    "message": "ArtifactStorage not configured"},
        )

    owner_scope = _resolve_owner_scope_from_user(user)
    url, actual_variant = await artifact_storage.get_presigned_download_url_for_variant(
        artifact_id, variant, expiry_seconds=expires_in, owner_scope=owner_scope,
    )
    if url is None or actual_variant is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found",
                    "message": f"artifact {artifact_id!r} not found"},
        )

    # Pull metadata for width/height/mime
    artifact = await artifact_storage.find_variant(artifact_id, actual_variant)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).isoformat()
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


class ImageTurnPublic(BaseModel):
    turn_id: str
    task_id: str | None
    prompt: str | None
    model_id: str | None
    style: str | None
    add_watermark: bool
    parent_artifact_id: str | None
    output_artifact_id: str | None
    status: str
    error: str | None
    error_code: str | None
    created_at: str
    completed_at: str | None
    output_url: str | None = None  # populated when include_urls=true


class ImageSessionResponse(BaseModel):
    session_id: str
    latest_artifact_id: str | None
    locked_style: str | None
    created_at: str
    updated_at: str
    turns: list[ImageTurnPublic]
    next_cursor: str | None = None


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
            detail={"error_code": "storage_unavailable",
                    "message": "Database pool not available"},
        )

    sess = await get_image_session(pool, session_id)
    if not sess:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found",
                    "message": f"image session {session_id!r} not found"},
        )

    requested_owner_scope = _resolve_owner_scope_from_user(user)
    session_owner_scope = sess.get("owner_scope")
    if session_owner_scope and session_owner_scope != requested_owner_scope:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found",
                    "message": f"image session {session_id!r} not found"},
        )
    rows, next_cursor = await list_turns(
        pool, session_id=session_id, owner_scope=requested_owner_scope,
        limit=limit, cursor=cursor,
    )

    artifact_storage = _get_artifact_storage() if include_urls else None
    turns_out: list[ImageTurnPublic] = []
    for row in rows:
        output_url: str | None = None
        oid = row.get("output_artifact_id")
        if include_urls and artifact_storage and oid:
            try:
                url, _ = await artifact_storage.get_presigned_download_url_for_variant(
                    oid, "display", owner_scope=requested_owner_scope,
                )
                output_url = url
            except Exception as exc:
                logger.warning("turn output URL resolve failed: %s", exc)
        created_at = row.get("created_at")
        completed_at = row.get("completed_at")
        turns_out.append(ImageTurnPublic(
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
        ))

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
