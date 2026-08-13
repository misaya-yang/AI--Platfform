"""Storage, replay, ownership, and persistence helpers for image routes."""

from __future__ import annotations

import asyncio
import base64 as _b64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.image import (
    append_image_turns,
    build_gemini_contents_from_history,
    create_image_blob,
    get_image_blob,
    inflate_history_with_bytes,
    make_thumbnail,
)
from ai_gateway_core.image import compute_owner_scope as _compute_owner_scope
from ai_gateway_core.logging import record_internal_exception
from ai_gateway_core.security import SafeFetchError
from ai_gateway_core.style_presets import compose_styled_prompt, resolve_style_preset
from fastapi import HTTPException, Request

from ...auth import UserContext
from .image_contracts import GeneratedImage, ImageGenerationRequest

logger = logging.getLogger("assistant_service.api.routes.images")

_REFERENCE_MAX_BYTES = int(os.getenv("IMAGE_REFERENCE_MAX_BYTES", str(8 * 1024 * 1024)))
_REPLAY_MAX_VISUAL_TURNS = int(os.getenv("IMAGE_REPLAY_MAX_VISUAL_TURNS", "4"))


def _get_artifact_storage():
    """Artifact storage lives in ai_gateway_core (since Phase 5f Batch B).
    Return None if the module isn't reachable (dev + tests) — callers fall
    back to data URLs in the response and skip session-history persistence
    (multi-turn won't work)."""
    try:
        from ai_gateway_core.storage import get_artifact_storage

        return get_artifact_storage()
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
        )
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
                detail={
                    "error_code": "validation_error",
                    "message": "Invalid data URL reference_image",
                },
            )
        if ";" in header:
            mime_type = header[5:].split(";", 1)[0] or mime_type
        raw = data
    try:
        decoded = _b64.b64decode(raw, validate=True)
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
        )
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
            detail={
                "error_code": "reference_too_large",
                "message": "reference blob exceeds size limit",
            },
        )
    expected_sha = row.get("content_sha256")
    if expected_sha and _sha256_hex(content) != expected_sha:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "blob_integrity_mismatch",
                "message": "reference blob checksum mismatch",
            },
        )
    return content


async def _persist_and_get_url_impl(
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
    watermark_fn,
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
            cb64, mt = await asyncio.to_thread(watermark_fn, raw_b64)
        else:
            cb64, mt = raw_b64, raw_mt
        return None, GeneratedImage(
            url=f"data:{mt};base64,{cb64}",
            width=width,
            height=height,
            artifact_id=None,
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
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", e
        )
        # Fall back to data URL as response, no artifact.
        if add_watermark:
            cb64, mt = await asyncio.to_thread(watermark_fn, raw_b64)
        else:
            cb64, mt = raw_b64, raw_mt
        return None, GeneratedImage(
            url=f"data:{mt};base64,{cb64}",
            width=width,
            height=height,
            artifact_id=None,
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
            record_internal_exception(
                __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
            )

    if not add_watermark:
        # Single-artifact happy path.
        return raw_artifact_id, GeneratedImage(
            url=raw_url or f"data:{raw_mt};base64,{raw_b64}",
            width=width,
            height=height,
            artifact_id=raw_artifact_id,
        )

    # add_watermark=True → store a separate watermarked artifact for the public URL.
    try:
        wm_b64, wm_mt = await asyncio.to_thread(watermark_fn, raw_b64)
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
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", e
        )
        # Degrade: return raw URL but still keep raw_artifact_id for history.
        return raw_artifact_id, GeneratedImage(
            url=raw_url or f"data:{raw_mt};base64,{raw_b64}",
            width=width,
            height=height,
            artifact_id=raw_artifact_id,
        )

    return raw_artifact_id, GeneratedImage(
        url=public_url or raw_url or f"data:{raw_mt};base64,{raw_b64}",
        width=width,
        height=height,
        artifact_id=public_artifact_id,
    )


async def _persist_and_get_url_bounded_impl(
    *args, bounded, semaphore, watermark_fn, **kwargs
) -> tuple[str | None, GeneratedImage]:
    async with bounded(
        semaphore,
        status_code=503,
        error_code="persistence_busy",
        message="Image persistence concurrency is saturated",
        retry_after=3,
    ):
        return await _persist_and_get_url_impl(*args, watermark_fn=watermark_fn, **kwargs)


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
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
        )
        try:
            raw = await artifact_storage.get_artifact(artifact_id)
        except Exception as exc2:
            record_internal_exception(
                __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc2
            )
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
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
        )
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


async def _resolve_reference_bytes_impl(
    body: ImageGenerationRequest,
    *,
    artifact_storage,
    user: UserContext | None = None,
    owner_scope: str | None = None,
    db_pool=None,
    safe_fetch_fn,
    persistent_task_store_required,
    get_image_session_fn,
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
                detail={
                    "error_code": "reference_too_large",
                    "message": "reference artifact exceeds size limit",
                },
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
                detail={
                    "error_code": "reference_too_large",
                    "message": "reference artifact exceeds size limit",
                },
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
            row = await get_image_session_fn(db_pool, body.session_id)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
            )
            row = None
        if row and row.get("latest_artifact_id"):
            latest_id = row["latest_artifact_id"]
            content = await _load_artifact_bytes_owner_scoped(
                artifact_storage,
                latest_id,
                owner_scope=owner_scope,
                user=user,
            )
            if len(content) > _REFERENCE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_code": "reference_too_large",
                        "message": "reference artifact exceeds size limit",
                    },
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
                if persistent_task_store_required():
                    raise
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
                )
                if persistent_task_store_required():
                    raise
        return ref_b64, None

    # 6. reference_image_url
    if not body.reference_image_url:
        return None, None

    try:
        content = await safe_fetch_fn(
            body.reference_image_url,
            max_bytes=8 * 1024 * 1024,
            max_redirects=3,
            timeout=30.0,
        )
        if len(content) > _REFERENCE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "error_code": "reference_too_large",
                    "message": "reference image exceeds size limit",
                },
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
                if persistent_task_store_required():
                    raise
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
                )
                if persistent_task_store_required():
                    raise
        return _b64.b64encode(content).decode(), None
    except SafeFetchError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=f"reference_image_url: {exc}",
        ) from exc
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
        )
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
        record_internal_exception(
            __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
        )
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
        is_visual_model = turn.get("role") == "model" and (
            turn.get("artifact_id") or turn.get("image_base64") or turn.get("file_uri")
        )
        if is_visual_model:
            if visual_seen >= _REPLAY_MAX_VISUAL_TURNS:
                continue
            visual_seen += 1
        keep_reversed.append(turn)
    return list(reversed(keep_reversed))


async def _run_gemini_multi_turn_impl(
    body: ImageGenerationRequest,
    *,
    aspect_ratio: str,
    width: int,
    height: int,
    session_manager,
    user: UserContext,
    artifact_storage,
    gemini_factory,
    bounded,
    provider_semaphore,
):
    """Run one turn of a stateful multi-turn editing session.

    Returns the raw Gemini result + the resolved style preset; the caller
    is responsible for persisting the new turn back to the session and for
    building the public response shape.
    """
    if not body.session_id:
        raise ValueError("session_id required for multi-turn flow")

    gemini = gemini_factory()
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

    effective_preset = body.style if body.style is not StylePreset.DEFAULT else locked_preset
    styled_prompt = compose_styled_prompt(body.prompt, effective_preset)

    # Inflate history pointers → real bytes for Gemini's inlineData.
    async def _download(aid: str) -> bytes | None:
        return await _download_artifact_bytes(artifact_storage, aid)

    bounded_history = _limit_replay_history(image_history)
    resolved_history = await inflate_history_with_bytes(bounded_history, _download)
    contents = build_gemini_contents_from_history(resolved_history, styled_prompt)

    async with bounded(
        provider_semaphore,
        status_code=429,
        error_code="provider_busy",
        message="Gemini image provider concurrency is saturated",
    ):
        res = await gemini.generate_chat(
            contents=contents,
            n=body.n,
            aspect_ratio=aspect_ratio,
        )
    return res, (session, image_history, effective_preset), None


async def _persist_multi_turn_result_impl(
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
    cap_result_images,
    persist_image,
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
    cap_result_images(res, body.n)

    persisted = await asyncio.gather(
        *[
            persist_image(
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
        ]
    )
    canonical_artifact_id = persisted[0][0]
    generated_list = [gi for _, gi in persisted]

    if write_legacy_metadata and session_manager and session:
        append_image_turns(
            image_history,
            body.prompt,
            res.images[0],
            res.text,
            artifact_id=canonical_artifact_id,
        )
        meta = dict(session.metadata or {})
        meta["image_chat_history"] = image_history
        meta["style_preset"] = effective_preset.value
        try:
            await session_manager.update_metadata(body.session_id, meta)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
            )

    return canonical_artifact_id, generated_list


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


async def _check_idempotency_impl(
    pool,
    *,
    owner_scope: str,
    body: ImageGenerationRequest,
    compute_request_hash,
    lookup_idempotent_fn,
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
    request_hash = compute_request_hash(_request_payload_for_hash(body))
    existing = await lookup_idempotent_fn(
        pool,
        owner_scope=owner_scope,
        client_request_id=body.client_request_id,
    )
    if existing:
        if existing["request_hash"] != request_hash:
            return None, True, request_hash
        return existing["task_id"], False, request_hash
    return None, False, request_hash


async def _ensure_image_session_impl(
    pool,
    *,
    session_id: str,
    owner_scope: str,
    user: UserContext,
    body: ImageGenerationRequest,
    get_image_session_fn,
    upsert_image_session_fn,
) -> dict | None:
    """Ensure ``image_sessions`` row exists for (session_id, owner_scope).

    Returns the row (post-upsert). Owner-scope mismatch on existing row →
    raises 404 (treat as nonexistent for the new owner).
    """
    if pool is None:
        return None
    existing = await get_image_session_fn(pool, session_id)
    if existing and existing.get("owner_scope") and existing.get("owner_scope") != owner_scope:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "not_found",
                "message": f"image session {session_id!r} not found",
            },
        )
    await upsert_image_session_fn(
        pool,
        session_id=session_id,
        owner_scope=owner_scope,
        app_user_id=body.app_user_id or user.app_user_id,
        app_tenant_id=body.app_tenant_id or user.app_tenant_id,
        locked_style=None,  # don't touch on upsert
    )
    return await get_image_session_fn(pool, session_id)


async def _check_expected_parent_impl(
    pool,
    *,
    session_id: str,
    expected_parent: str | None,
    get_image_session_fn,
) -> None:
    """If caller provided ``expected_parent_artifact_id``, verify it matches
    the session's current ``latest_artifact_id``. Raises 409 on mismatch."""
    if not expected_parent or pool is None:
        return
    row = await get_image_session_fn(pool, session_id)
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


async def _resolve_style_for_session_impl(
    pool,
    *,
    session_id: str | None,
    body_style: StylePreset,
    style_explicit: bool,
    get_image_session_fn,
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
    row = await get_image_session_fn(pool, session_id)
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


async def _record_turn_impl(
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
    insert_turn_fn,
) -> None:
    """Persist a row into image_turns. session_id-less stateless turns get
    a synthetic session id so the row's NOT NULL constraint holds, but they
    won't show up in any /image-sessions/{id} listing."""
    if pool is None:
        return
    effective_session = session_id or f"stateless_{turn_id}"
    await insert_turn_fn(
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
                raw_artifact_id,
                v,
                owner_scope=owner_scope,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.api.routes.image_route_helpers.internal_failure", exc
            )
            continue
        if url and actual:
            out[v] = url
    return out or None
