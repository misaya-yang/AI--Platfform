"""
Presigned URL API for Direct Upload.

Part of P2: Architecture Decoupling

Provides presigned URLs for direct upload to S3/OSS, bypassing the backend
for large file uploads. This reduces server load and enables:
- Transfer Acceleration
- Resumable uploads
- Larger file size limits

Flow:
1. Client requests presigned URL with filename and content type
2. Backend validates document ownership and permissions
3. Backend generates presigned URL with expiry
4. Client uploads directly to S3/OSS
5. Client notifies backend of completion (with upload_id verification)
6. Backend queues async processing (VLM description, embedding, etc.)

Security:
- Document ownership validation before URL generation
- Upload session tracking (upload_id → user/document/storage_key)
- Storage key prefix validation on confirm
"""

from __future__ import annotations

from datetime import datetime, timezone

from ai_gateway_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ..deps import get_user_context

logger = get_logger(__name__)

# In-memory upload session cache (TTL-based cleanup)
# For production, consider using Redis or database
_upload_sessions: dict[str, dict] = {}

# Maximum number of sessions to prevent memory exhaustion
# In production, use Redis with TTL instead
MAX_UPLOAD_SESSIONS = 10000
_DIRECT_UPLOAD_UNAVAILABLE = (
    "Direct presigned upload is not implemented; use the standard upload API."
)

router = APIRouter(prefix="/presign", tags=["Presigned Upload"])


# ============ Helper Functions ============


def _get_effective_tenant_id(user: UserContext) -> str:
    """Get effective tenant_id, falling back to user_id if tenant_id is empty."""
    return user.tenant_id or user.user_id


def _cleanup_expired_sessions() -> None:
    """Remove expired upload sessions from cache.

    Also enforces MAX_UPLOAD_SESSIONS limit by removing oldest sessions
    if the cache exceeds the limit.
    """
    now = datetime.now(timezone.utc)

    # Remove expired sessions
    expired = [
        upload_id
        for upload_id, session in _upload_sessions.items()
        if session.get("expires_at", now) < now
    ]
    for upload_id in expired:
        del _upload_sessions[upload_id]

    if expired:
        logger.info(f"Cleaned up {len(expired)} expired upload sessions")

    # Enforce max size limit (FIFO eviction if still over limit)
    if len(_upload_sessions) >= MAX_UPLOAD_SESSIONS:
        # Sort by created_at and remove oldest
        sorted_sessions = sorted(
            _upload_sessions.items(), key=lambda x: x[1].get("created_at", now)
        )
        to_remove = len(_upload_sessions) - MAX_UPLOAD_SESSIONS + 100  # Remove 100 extra
        for upload_id, _ in sorted_sessions[:to_remove]:
            del _upload_sessions[upload_id]

        logger.warning(
            f"Upload session cache exceeded {MAX_UPLOAD_SESSIONS}, "
            f"evicted {to_remove} oldest sessions"
        )


async def _validate_document_access(
    request: Request,
    document_id: str,
    user: UserContext,
) -> bool:
    """
    Validate user has access to the document.

    Checks:
    1. Document exists
    2. Document's dataset is accessible by user (tenant_id match or permission)

    Returns True if access is allowed, raises HTTPException otherwise.
    """
    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        # Fail closed to avoid issuing presigned URLs without ownership validation
        logger.error("Database not available for document access validation")
        raise HTTPException(
            status_code=503,
            detail="Database unavailable for document access validation",
        )

    try:
        # Get document
        document = await db.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        # Get dataset to check tenant ownership
        dataset_id = document.get("dataset_id")
        if not dataset_id:
            raise HTTPException(status_code=400, detail="Document has no associated dataset")

        dataset = await db.get_dataset(dataset_id)
        if not dataset:
            raise HTTPException(status_code=404, detail=f"Dataset not found: {dataset_id}")

        # Check tenant ownership
        dataset_tenant = dataset.get("tenant_id", "")
        effective_tenant = _get_effective_tenant_id(user)

        # Allow if:
        # 1. Dataset is public (visibility == "public")
        # 2. Dataset tenant matches user tenant
        # 3. User is admin
        visibility = dataset.get("visibility", "private")
        if visibility == "public":
            return True

        if "admin" in user.roles:
            return True

        if dataset_tenant and dataset_tenant == effective_tenant:
            return True

        # Check dataset permissions
        permissions = await db.get_dataset_permissions(dataset_id)
        for perm in permissions:
            if perm.get("subject_type") == "user" and perm.get("subject_id") == user.user_id:
                return True
            if perm.get("subject_type") == "role" and perm.get("subject_id") in user.roles:
                return True

        raise HTTPException(status_code=403, detail="Access denied to document")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to validate document access: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to validate document access: {str(e)}")


# ============ Schemas ============


class PresignedUploadRequest(BaseModel):
    """Request for presigned upload URL."""

    filename: str = Field(..., description="Original filename", max_length=255)
    content_type: str = Field(..., description="MIME type of the file")
    document_id: str = Field(..., description="Document ID to associate with")
    file_size_bytes: int | None = Field(None, description="Expected file size in bytes")
    metadata: dict | None = Field(None, description="Optional metadata")


class PresignedUploadResponse(BaseModel):
    """Response with presigned upload URL."""

    upload_url: str = Field(..., description="Presigned URL for PUT upload")
    method: str = Field(default="PUT", description="HTTP method to use")
    headers: dict = Field(default_factory=dict, description="Required headers for upload")
    storage_key: str = Field(..., description="Storage key for the uploaded file")
    upload_id: str = Field(..., description="Unique ID for this upload session")
    expiry_seconds: int = Field(..., description="URL expiry time in seconds")
    expires_at: str = Field(..., description="URL expiry timestamp ISO8601")


class UploadConfirmRequest(BaseModel):
    """Request to confirm upload completion."""

    upload_id: str = Field(..., description="Upload ID from presigned URL response")
    storage_key: str = Field(..., description="Storage key from presigned URL response")
    document_id: str = Field(..., description="Document ID")
    filename: str = Field(..., description="Original filename")
    content_type: str = Field(..., description="MIME type")
    file_size_bytes: int | None = Field(None, description="Actual file size")


class UploadConfirmResponse(BaseModel):
    """Response after confirming upload."""

    status: str = Field(..., description="Status: processing, queued, error")
    task_id: str | None = Field(None, description="Task ID for async processing")
    message: str = Field(..., description="Status message")
    storage_url: str = Field(..., description="Final storage URL")


# ============ Endpoints ============


@router.post("/upload", response_model=PresignedUploadResponse, deprecated=True)
async def get_presigned_upload_url(
    request: PresignedUploadRequest,
    user: UserContext = Depends(get_user_context),
):
    """Reject the incomplete direct-upload flow before creating storage state."""
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    raise HTTPException(status_code=501, detail=_DIRECT_UPLOAD_UNAVAILABLE)


@router.post("/confirm", response_model=UploadConfirmResponse, deprecated=True)
async def confirm_upload(
    request: UploadConfirmRequest,
    user: UserContext = Depends(get_user_context),
):
    """Reject confirmation because direct uploads are never issued."""
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    raise HTTPException(status_code=501, detail=_DIRECT_UPLOAD_UNAVAILABLE)


@router.get("/status/{task_id}", deprecated=True)
async def get_task_status(
    task_id: str,
    user: UserContext = Depends(get_user_context),
):
    """Reject status lookup because the direct-upload queue is not implemented."""
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    raise HTTPException(status_code=501, detail=_DIRECT_UPLOAD_UNAVAILABLE)
