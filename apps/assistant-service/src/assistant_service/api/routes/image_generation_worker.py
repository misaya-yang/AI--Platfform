"""Background image-generation worker extracted from the route module."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.image import new_turn_id, parse_image_size, resolve_image_routing
from ai_gateway_core.style_presets import (
    compose_styled_prompt,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
)
from fastapi import HTTPException

from ...auth import UserContext
from ...core.models.model_registry import ModelRegistry
from .image_contracts import AsyncImageGenerationRequest, GeneratedImage

logger = logging.getLogger("assistant_service.api.routes.images")


@dataclass(frozen=True)
class ImageWorkerBindings:
    """Route-local dependencies whose bindings are intentionally patchable in tests."""

    load_task: Any
    store_task: Any
    resolve_owner_scope: Any
    update_turn_status: Any
    get_artifact_storage: Any
    get_image_session: Any
    resolve_reference_bytes: Any
    resolve_style_for_session: Any
    run_gemini_multi_turn: Any
    get_gemini_image_generator: Any
    get_smart_image_generator: Any
    bounded: Any
    provider_semaphore: Any
    cap_result_images: Any
    persist_multi_turn_result: Any
    post_generation_bookkeeping: Any
    persist_and_get_url_bounded: Any
    send_image_callback: Any


async def run_image_generation_task(
    task_id: str,
    body: AsyncImageGenerationRequest,
    model_registry: ModelRegistry,
    user: UserContext,
    session_manager=None,
    redis=None,
    pool=None,
    *,
    bindings: ImageWorkerBindings,
) -> None:
    task = await bindings.load_task(redis, task_id)
    if task is None:
        # Defensive: caller should have stored the task before scheduling us.
        logger.error("Async image task %s vanished before worker started", task_id)
        return
    task["status"] = "running"
    task["progress"] = 10
    await bindings.store_task(redis, task_id, task, pool=pool)
    start_time = time.time()

    owner_scope = task.get("owner_scope") or bindings.resolve_owner_scope(user, body)
    turn_id = task.get("turn_id") or new_turn_id()
    request_hash = task.get("request_hash")
    resolved_parent: str | None = None
    raw_anchor: str | None = None

    # Mark turn as running for parallel observers
    with suppress(Exception):
        await bindings.update_turn_status(pool, turn_id=turn_id, status="running")

    try:
        model_info = model_registry.get_model(body.model_id) if model_registry else None
        selected_provider = model_info.provider.value if model_info else None
        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            body.model_id,
            selected_provider,
        )
        width, height, aspect_ratio = parse_image_size(body.size)
        task["progress"] = 30

        artifact_storage = bindings.get_artifact_storage()
        has_explicit_ref = bool(
            body.parent_artifact_id
            or body.reference_artifact_id
            or body.reference_blob_id
            or body.reference_image
            or body.reference_image_url
        )
        session_implies_reference = False
        if body.session_id and pool is not None and not has_explicit_ref:
            sess_row = await bindings.get_image_session(pool, body.session_id)
            session_implies_reference = bool(sess_row and sess_row.get("latest_artifact_id"))
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
            await bindings.store_task(redis, task_id, task, pool=pool)
            await bindings.update_turn_status(
                pool,
                turn_id=turn_id,
                status="failed",
                error=task["error"],
                error_code="reference_requires_gemini",
            )
            if body.callback_url:
                try:
                    await bindings.send_image_callback(body.callback_url, task)
                except Exception as e:
                    logger.warning("Callback to %s failed: %s", body.callback_url, e)
            return

        # Style lock resolution
        style_explicit = "style" in body.model_fields_set
        effective_style, new_locked_style, clear_lock = await bindings.resolve_style_for_session(
            pool,
            session_id=body.session_id,
            body_style=body.style,
            style_explicit=style_explicit,
        )

        # Resolve reference bytes (may raise 404 → recorded as failed turn)
        ref_b64: str | None = None
        if prefer_gemini and has_reference:
            try:
                ref_b64, resolved_parent = await bindings.resolve_reference_bytes(
                    body,
                    artifact_storage=artifact_storage,
                    user=user,
                    owner_scope=owner_scope,
                    db_pool=pool,
                )
            except HTTPException as exc:
                msg = exc.detail if isinstance(exc.detail, str) else "reference not found"
                task["status"] = "failed"
                task["error"] = msg
                task["error_code"] = "reference_not_found"
                task["http_status_code"] = exc.status_code
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await bindings.store_task(redis, task_id, task, pool=pool)
                await bindings.update_turn_status(
                    pool,
                    turn_id=turn_id,
                    status="failed",
                    error=msg,
                    error_code="reference_not_found",
                )
                if body.callback_url:
                    with suppress(Exception):
                        await bindings.send_image_callback(body.callback_url, task)
                return

        res = None
        generated_response_imgs: list[GeneratedImage] = []
        provider_label: str | None = None
        styled_prompt = compose_styled_prompt(body.prompt, effective_style)

        if prefer_gemini and ref_b64 is not None:
            gemini = bindings.get_gemini_image_generator()
            if not gemini.is_configured:
                task["status"] = "failed"
                task["error"] = "Gemini API key not configured"
                task["error_code"] = "provider_unavailable"
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await bindings.store_task(redis, task_id, task, pool=pool)
                await bindings.update_turn_status(
                    pool,
                    turn_id=turn_id,
                    status="failed",
                    error=task["error"],
                    error_code="provider_unavailable",
                )
                if body.callback_url:
                    with suppress(Exception):
                        await bindings.send_image_callback(body.callback_url, task)
                return
            async with bindings.bounded(
                bindings.provider_semaphore,
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

        if res is None and body.session_id and session_manager and prefer_gemini:
            # Legacy session-history backed flow (no explicit parent)
            res, session_state, err = await bindings.run_gemini_multi_turn(
                body,
                aspect_ratio=aspect_ratio,
                width=width,
                height=height,
                session_manager=session_manager,
                user=user,
                artifact_storage=artifact_storage,
            )
            if err:
                task["status"] = "failed"
                task["error"] = err
                task["error_code"] = "provider_unavailable"
                task["progress"] = 100
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                await bindings.store_task(redis, task_id, task, pool=pool)
                await bindings.update_turn_status(
                    pool,
                    turn_id=turn_id,
                    status="failed",
                    error=err,
                    error_code="provider_unavailable",
                )
                if body.callback_url:
                    with suppress(Exception):
                        await bindings.send_image_callback(body.callback_url, task)
                return
            if res and res.success and res.images:
                raw_anchor, generated_list = await bindings.persist_multi_turn_result(
                    body,
                    res=res,
                    session_state=session_state,
                    session_manager=session_manager,
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
                generated_response_imgs.extend(generated_list)
                provider_label = "google"

        if res is None:
            dashscope_tag = resolve_dashscope_style_tag(effective_style)
            negative_prompt = resolve_negative_prompt(effective_style)
            router_svc = bindings.get_smart_image_generator()
            async with bindings.bounded(
                bindings.provider_semaphore,
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

        bindings.cap_result_images(res, body.n)
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
            await bindings.store_task(redis, task_id, task, pool=pool)
            await bindings.update_turn_status(
                pool,
                turn_id=turn_id,
                status="failed",
                error=err,
                error_code=error_code,
            )
            if body.callback_url:
                with suppress(Exception):
                    await bindings.send_image_callback(body.callback_url, task)
            return

        task["progress"] = 70

        if not generated_response_imgs:
            persisted = await asyncio.gather(
                *[
                    bindings.persist_and_get_url_bounded(
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
        latest_advanced = await bindings.post_generation_bookkeeping(
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
            await bindings.update_turn_status(
                pool,
                turn_id=turn_id,
                status="failed",
                error=task["error"],
                error_code=task["error_code"],
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
            await bindings.update_turn_status(
                pool,
                turn_id=turn_id,
                status="failed",
                error=str(e),
                error_code="internal_error",
            )

    await bindings.store_task(redis, task_id, task, pool=pool)

    if body.callback_url:
        try:
            await bindings.send_image_callback(body.callback_url, task)
        except Exception as e:
            logger.warning("Callback to %s failed: %s", body.callback_url, e)
