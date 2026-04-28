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
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.image import (
    advance_latest_artifact_cas,
    append_image_turns,
    apply_watermark_b64,
    build_gemini_contents_from_history,
    compute_owner_scope as _compute_owner_scope,
    compute_request_hash as _compute_request_hash,
    get_image_session,
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
    set_locked_style as _set_locked_style,
    update_turn_status,
    upsert_image_session,
)
from ai_gateway_core.security import SafeFetchError, safe_fetch
from ai_gateway_core.style_presets import (
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
    # Owner check: prefer owner_scope when set (and a real string — pre-mig
    # rows / test mocks may carry a non-string sentinel), else fall back to
    # legacy tenant_id+user_id check.
    owner_ok = False
    raw_scope = getattr(raw, "owner_scope", None)
    if isinstance(raw_scope, str) and raw_scope:
        owner_ok = raw_scope == owner_scope
    elif user is not None:
        owner_ok = raw.user_id == user.user_id and raw.tenant_id == user.tenant_id
    if not owner_ok:
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
    3. session-derived latest_artifact (when ``session_id`` set + image_session
       row exists with a non-null ``latest_artifact_id``).
    4. ``reference_image`` (base64 / data-URL) — pass through.
    5. ``reference_image_url`` — SSRF-safe fetch.

    Returns ``(None, None)`` if no reference is provided.
    """
    # 1. parent_artifact_id (explicit anchor)
    if body.parent_artifact_id:
        content = await _load_artifact_bytes_owner_scoped(
            artifact_storage,
            body.parent_artifact_id,
            owner_scope=owner_scope or (user.user_id if user else ""),
            user=user,
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
        return _b64.b64encode(content).decode(), body.reference_artifact_id

    # 3. session-derived latest
    if body.session_id and db_pool is not None and owner_scope is not None:
        try:
            row = await get_image_session(db_pool, body.session_id)
        except Exception as exc:
            logger.warning("image_session lookup failed: %s", exc)
            row = None
        if row and row.get("owner_scope") == owner_scope and row.get("latest_artifact_id"):
            latest_id = row["latest_artifact_id"]
            content = await _load_artifact_bytes_owner_scoped(
                artifact_storage, latest_id,
                owner_scope=owner_scope, user=user,
            )
            return _b64.b64encode(content).decode(), latest_id

    # 4. raw base64 / data URL
    if body.reference_image:
        return body.reference_image, None

    # 5. reference_image_url
    if not body.reference_image_url:
        return None, None

    try:
        content = await safe_fetch(
            body.reference_image_url,
            max_bytes=8 * 1024 * 1024,
            max_redirects=3,
            timeout=30.0,
        )
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
    if existing and existing.get("owner_scope") != owner_scope:
        raise HTTPException(
            status_code=404,
            detail=f"Session {session_id!r} not found",
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
# POST /generate-image — synchronous
# -----------------------------------------------------------------------------


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
                sess_row
                and sess_row.get("owner_scope") == owner_scope
                and sess_row.get("latest_artifact_id")
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
            res = await router_svc.generate(
                prompt=styled_prompt, n=body.n, size=body.size or "1024*1024",
                style=dashscope_tag, negative_prompt=negative_prompt,
                aspect_ratio=aspect_ratio,
                prefer_gemini=prefer_gemini, prefer_doubao=prefer_doubao,
                dashscope_model=dashscope_model,
            )
            provider_label = res.provider

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
            _persist_and_get_url(
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
        try:
            await _record_turn(
                pool, turn_id=turn_id, session_id=body.session_id,
                owner_scope=owner_scope, task_id=None, body=body,
                parent_artifact_id=None, output_artifact_id=None,
                status="failed", error=str(e), error_code="internal_error",
                request_hash=None,
            )
        except Exception:
            pass
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
    await _store_task(redis, task_id, task)
    start_time = time.time()

    owner_scope = task.get("owner_scope") or _resolve_owner_scope(user, body)
    turn_id = task.get("turn_id") or new_turn_id()
    request_hash = task.get("request_hash")
    resolved_parent: str | None = None
    raw_anchor: str | None = None

    # Mark turn as running for parallel observers
    try:
        await update_turn_status(pool, turn_id=turn_id, status="running")
    except Exception:
        pass

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
            or body.reference_image
            or body.reference_image_url
        )
        session_implies_reference = False
        if (
            body.session_id and pool is not None and not has_explicit_ref
        ):
            sess_row = await get_image_session(pool, body.session_id)
            session_implies_reference = bool(
                sess_row and sess_row.get("owner_scope") == owner_scope
                and sess_row.get("latest_artifact_id")
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
            await _store_task(redis, task_id, task)
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
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await _store_task(redis, task_id, task)
                await update_turn_status(
                    pool, turn_id=turn_id, status="failed",
                    error=msg, error_code="reference_not_found",
                )
                if body.callback_url:
                    try:
                        await send_image_callback(body.callback_url, task)
                    except Exception:
                        pass
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
                await _store_task(redis, task_id, task)
                await update_turn_status(
                    pool, turn_id=turn_id, status="failed",
                    error=task["error"], error_code="provider_unavailable",
                )
                if body.callback_url:
                    try:
                        await send_image_callback(body.callback_url, task)
                    except Exception:
                        pass
                return
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
                await _store_task(redis, task_id, task)
                await update_turn_status(
                    pool, turn_id=turn_id, status="failed",
                    error=err, error_code="provider_unavailable",
                )
                if body.callback_url:
                    try:
                        await send_image_callback(body.callback_url, task)
                    except Exception:
                        pass
                return
            if res and res.success and res.images:
                raw_anchor, generated_list = await _persist_multi_turn_result(
                    body, res=res, session_state=session_state,
                    session_manager=session_manager, user=user,
                    artifact_storage=artifact_storage,
                    width=width, height=height,
                )
                generated_response_imgs.extend(generated_list)
                provider_label = "google"

        if res is None:
            dashscope_tag = resolve_dashscope_style_tag(effective_style)
            negative_prompt = resolve_negative_prompt(effective_style)
            router_svc = get_smart_image_generator()
            res = await router_svc.generate(
                prompt=styled_prompt, n=body.n, size=body.size or "1024*1024",
                style=dashscope_tag, negative_prompt=negative_prompt,
                aspect_ratio=aspect_ratio,
                prefer_gemini=prefer_gemini, prefer_doubao=prefer_doubao,
                dashscope_model=dashscope_model,
            )
            provider_label = res.provider

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
            await _store_task(redis, task_id, task)
            await update_turn_status(
                pool, turn_id=turn_id, status="failed",
                error=err, error_code=error_code,
            )
            if body.callback_url:
                try:
                    await send_image_callback(body.callback_url, task)
                except Exception:
                    pass
            return

        task["progress"] = 70

        if not generated_response_imgs:
            persisted = await asyncio.gather(*[
                _persist_and_get_url(
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

    except Exception as e:
        logger.exception("Async image generation task %s failed", task_id)
        task["status"] = "failed"
        task["error"] = str(e)
        task["error_code"] = "internal_error"
        task["progress"] = 100
        task["duration_ms"] = (time.time() - start_time) * 1000
        task["completed_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await update_turn_status(
                pool, turn_id=turn_id, status="failed",
                error=str(e), error_code="internal_error",
            )
        except Exception:
            pass

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
    pool = _get_db_pool(request)
    owner_scope = _resolve_owner_scope(user, body)

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
        return AsyncImageTaskSubmitResponse(
            task_id=replay_task_id, status="pending",
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
                return AsyncImageTaskSubmitResponse(
                    task_id=existing["task_id"], status="pending",
                    message="Idempotent replay — existing task returned",
                )
            if existing and existing["request_hash"] != request_hash:
                # extremely unlikely race: lookup at top said no row, but
                # by the time we tried to insert, a concurrent submit with
                # a *different* body got there first. Surface as conflict.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "idempotency_conflict",
                        "message": "client_request_id raced with a different request body",
                    },
                )
            # No existing row but our INSERT failed for a different reason
            # (transient DB) — fall through and run anyway. The audit row
            # may end up missing, but the user request still completes.

    redis = getattr(request.app.state, "redis", None)
    await _store_task(redis, task_id, task)

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
    except Exception as exc:
        logger.warning("insert pending turn failed (artifact=%s): %s", task_id, exc)

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
            turn = await get_turn_by_task(pool, task_id)
            if turn:
                # Owner-scope check: deny cross-owner access (404 to hide existence)
                expected_scope = _compute_owner_scope(
                    user.user_id,
                    app_tenant_id=user.app_tenant_id,
                    app_user_id=user.app_user_id,
                )
                # Allow only when owner_scope matches OR is NULL (legacy
                # pre-mig rows). Do NOT additionally accept user.user_id —
                # that would let a delegated app reading on behalf of one
                # end-user read another's tasks if the JWT subject equals
                # the latter's task owner_scope. expected_scope already
                # collapses to user.user_id for legacy callers (no app
                # headers), so dropping the third branch is safe.
                turn_owner = turn.get("owner_scope")
                if turn_owner is not None and turn_owner != expected_scope:
                    raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

                # Build a synthetic task dict from the turn row
                images: list[AsyncImageArtifact] = []
                output_id = turn.get("output_artifact_id")
                if output_id:
                    artifact_storage = _get_artifact_storage()
                    if artifact_storage:
                        try:
                            url, _actual = await artifact_storage.get_presigned_download_url_for_variant(
                                output_id, "display",
                                owner_scope=turn.get("owner_scope") or user.user_id,
                            )
                        except Exception:
                            url = None
                        if url:
                            images = [AsyncImageArtifact(
                                artifact_id=output_id,
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
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Ownership check — return 404 (not 403) to avoid leaking task existence
    # via timing/error-code distinction.
    owner_user_id = task.get("owner_user_id")
    owner_tenant_id = task.get("owner_tenant_id")
    if owner_user_id is not None and owner_user_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if owner_tenant_id is not None and owner_tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    # owner_scope check (Phase 2): when present on task, must match request scope.
    task_scope = task.get("owner_scope")
    if task_scope is not None:
        expected_scope = _compute_owner_scope(
            user.user_id,
            app_tenant_id=user.app_tenant_id,
            app_user_id=user.app_user_id,
        )
        if task_scope != expected_scope:
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

    # Read app-scope from headers (and let callers override via query for
    # multi-tenant proxies that pass app_user_id separately is not in scope
    # here — they should set X-App-* headers).
    owner_scope = _compute_owner_scope(
        user.user_id,
        app_tenant_id=user.app_tenant_id,
        app_user_id=user.app_user_id,
    )

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

    owner_scope = _compute_owner_scope(
        user.user_id,
        app_tenant_id=user.app_tenant_id,
        app_user_id=user.app_user_id,
    )

    sess = await get_image_session(pool, session_id)
    if not sess or sess.get("owner_scope") != owner_scope:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_found",
                    "message": f"image session {session_id!r} not found"},
        )

    rows, next_cursor = await list_turns(
        pool, session_id=session_id, owner_scope=owner_scope,
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
                    oid, "display", owner_scope=owner_scope,
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
