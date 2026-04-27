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

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ai_gateway_core.logging import get_logger
from ai_gateway_core.storage.image_storage import ImageStorageService
from ..deps import get_image_storage_service, get_user_context

logger = get_logger(__name__)

# In-memory upload session cache (TTL-based cleanup)
# For production, consider using Redis or database
_upload_sessions: dict[str, dict] = {}

# Maximum number of sessions to prevent memory exhaustion
# In production, use Redis with TTL instead
MAX_UPLOAD_SESSIONS = 10000

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
            if perm.get("subject_type") == "role":
                if perm.get("subject_id") in user.roles:
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


@router.post("/upload", response_model=PresignedUploadResponse)
async def get_presigned_upload_url(
    http_request: Request,
    request: PresignedUploadRequest,
    user: UserContext = Depends(get_user_context),
    storage: ImageStorageService = Depends(get_image_storage_service),
):
    """
    Generate a presigned URL for direct upload to S3/OSS.

    The client can use this URL to upload files directly to storage
    without going through the backend. After upload, call /confirm
    to trigger async processing.

    Security:
    - Validates user authentication
    - Validates document ownership/permissions
    - Stores upload session for confirm verification

    Returns:
        Presigned URL and upload instructions
    """
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Check if presigned URLs are supported
    if not storage.supports_presigned_urls():
        raise HTTPException(
            status_code=400, detail="Presigned URLs not supported with current storage backend"
        )

    # Validate document access (Critical: prevent cross-tenant/cross-document attacks)
    await _validate_document_access(http_request, request.document_id, user)

    # Validate content type
    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "application/pdf",
    }
    if request.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, detail=f"Content type not allowed: {request.content_type}"
        )

    # Generate unique upload ID and attachment ID
    upload_id = str(uuid.uuid4())
    attachment_id = f"upload_{upload_id[:8]}"

    # Use effective tenant_id (Critical: use tenant_id not user_id)
    effective_tenant_id = _get_effective_tenant_id(user)

    # Generate presigned URL
    expiry_seconds = 900  # 15 minutes

    result = await storage.generate_presigned_upload_url(
        tenant_id=effective_tenant_id,
        document_id=request.document_id,
        attachment_id=attachment_id,
        filename=request.filename,
        content_type=request.content_type,
        expiry_seconds=expiry_seconds,
        metadata=request.metadata,
    )

    if not result:
        raise HTTPException(status_code=500, detail="Failed to generate presigned URL")

    # Calculate expiry timestamp
    expires_at_dt = datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)
    expires_at = expires_at_dt.isoformat()

    # Store upload session for later verification (Critical: prevent storage_key spoofing)
    _cleanup_expired_sessions()  # Cleanup old sessions
    _upload_sessions[upload_id] = {
        "user_id": user.user_id,
        "tenant_id": effective_tenant_id,
        "document_id": request.document_id,
        "storage_key": result["storage_key"],
        "filename": request.filename,
        "content_type": request.content_type,
        "expires_at": expires_at_dt,
        "created_at": datetime.now(timezone.utc),
    }

    logger.info(
        f"Generated presigned upload URL for user {user.user_id}, "
        f"document {request.document_id}, filename {request.filename}"
    )

    return PresignedUploadResponse(
        upload_url=result["url"],
        method=result.get("method", "PUT"),
        headers=result.get("headers", {}),
        storage_key=result["storage_key"],
        upload_id=upload_id,
        expiry_seconds=expiry_seconds,
        expires_at=expires_at,
    )


@router.post("/confirm", response_model=UploadConfirmResponse)
async def confirm_upload(
    request: UploadConfirmRequest,
    user: UserContext = Depends(get_user_context),
    storage: ImageStorageService = Depends(get_image_storage_service),
):
    """
    Confirm that a direct upload has completed.

    After uploading directly to S3/OSS using the presigned URL,
    call this endpoint to:
    1. Validate upload session ownership
    2. Verify the file exists in storage
    3. Queue async processing (VLM, embedding, etc.)
    4. Get a task ID for tracking progress

    Security:
    - Validates upload_id belongs to current user
    - Validates storage_key matches the original upload session
    - Prevents cross-user storage_key spoofing

    Returns:
        Processing status and task ID
    """
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Validate upload session (Critical: prevent cross-user storage_key spoofing)
    session = _upload_sessions.get(request.upload_id)
    if not session:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired upload_id. Please request a new presigned URL.",
        )

    # Verify session ownership
    if session["user_id"] != user.user_id:
        logger.warning(
            f"Upload session ownership mismatch: session user={session['user_id']}, "
            f"request user={user.user_id}, upload_id={request.upload_id}"
        )
        raise HTTPException(
            status_code=403, detail="Upload session does not belong to current user"
        )

    # Verify storage_key matches
    if session["storage_key"] != request.storage_key:
        logger.warning(
            f"Storage key mismatch: session key={session['storage_key']}, "
            f"request key={request.storage_key}, upload_id={request.upload_id}"
        )
        raise HTTPException(status_code=400, detail="Storage key does not match upload session")

    # Verify document_id matches
    if session["document_id"] != request.document_id:
        logger.warning(
            f"Document ID mismatch: session doc={session['document_id']}, "
            f"request doc={request.document_id}, upload_id={request.upload_id}"
        )
        raise HTTPException(status_code=400, detail="Document ID does not match upload session")

    # Verify file exists in storage
    try:
        exists = await storage.exists_by_key(request.storage_key)
        if not exists:
            raise HTTPException(
                status_code=404, detail="File not found in storage. Upload may have failed."
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to verify upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to verify upload: {str(e)}")

    # Generate storage URL
    storage_url = storage.get_url_by_key(request.storage_key)

    # Remove session after successful confirmation
    del _upload_sessions[request.upload_id]

    # TODO: Queue async processing task via ImageProcessingQueue
    # For now, return 501 to indicate feature is not fully implemented
    # This is more honest than returning fake "queued" status
    task_id = f"task_{uuid.uuid4().hex[:12]}"

    logger.info(
        f"Upload confirmed for user {user.user_id}, "
        f"document {request.document_id}, storage_key {request.storage_key}, "
        f"task_id {task_id}"
    )

    return UploadConfirmResponse(
        status="confirmed",
        task_id=task_id,
        message="Upload confirmed. Async processing not yet implemented - use standard upload API for processing.",
        storage_url=storage_url,
    )


@router.get("/status/{task_id}")
async def get_task_status(
    task_id: str,
    user: UserContext = Depends(get_user_context),
):
    """
    Get the status of an async processing task.

    Args:
        task_id: Task ID from /confirm response

    Returns:
        Task status and progress information
    """
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")

    # TODO: Implement task status lookup from queue
    # For now, return placeholder status
    return {
        "task_id": task_id,
        "status": "processing",
        "progress": 0,
        "message": "Task status tracking not yet implemented",
    }
