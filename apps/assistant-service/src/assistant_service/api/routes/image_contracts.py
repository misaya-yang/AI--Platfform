"""Pydantic contracts for the image generation routes."""

from __future__ import annotations

from typing import Any

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.style_presets import resolve_style_preset
from pydantic import BaseModel, Field, field_validator


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
        description=("Tenant id of the calling app's end-user. See `app_user_id`."),
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
