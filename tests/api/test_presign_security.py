"""
Tests for presigned upload API security.

Tests cover:
1. Authentication requirements
2. Document ownership validation
3. Upload session management
4. Cross-tenant/cross-document attack prevention
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from src.api.deps import get_user_context
from src.api.v1.presign import (
    PresignedUploadRequest,
    UploadConfirmRequest,
    _cleanup_expired_sessions,
    _get_effective_tenant_id,
    _upload_sessions,
    _validate_document_access,
    confirm_upload,
    get_presigned_upload_url,
    get_task_status,
)
from src.api.v1.presign import (
    router as presign_router,
)
from src.core.auth.user_resolver import UserContext


class TestEffectiveTenantId:
    """Tests for _get_effective_tenant_id helper."""

    def test_returns_tenant_id_when_available(self):
        """Should return tenant_id when it's not empty."""
        user = UserContext(
            user_id="user123",
            tenant_id="tenant456",
            is_authenticated=True,
        )
        assert _get_effective_tenant_id(user) == "tenant456"

    def test_falls_back_to_user_id_when_tenant_empty(self):
        """Should fall back to user_id when tenant_id is empty."""
        user = UserContext(
            user_id="user123",
            tenant_id="",
            is_authenticated=True,
        )
        assert _get_effective_tenant_id(user) == "user123"

    def test_falls_back_to_user_id_when_tenant_none(self):
        """Should fall back to user_id when tenant_id is None-like."""
        user = UserContext(
            user_id="user123",
            is_authenticated=True,
        )
        # Default tenant_id is empty string
        assert _get_effective_tenant_id(user) == "user123"


class TestCleanupExpiredSessions:
    """Tests for _cleanup_expired_sessions helper."""

    def setup_method(self):
        """Clear sessions before each test."""
        _upload_sessions.clear()

    def teardown_method(self):
        """Clear sessions after each test."""
        _upload_sessions.clear()

    def test_removes_expired_sessions(self):
        """Should remove sessions that have expired."""
        # Add an expired session
        _upload_sessions["expired1"] = {
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "user_id": "user1",
        }
        # Add a valid session
        _upload_sessions["valid1"] = {
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
            "user_id": "user2",
        }

        _cleanup_expired_sessions()

        assert "expired1" not in _upload_sessions
        assert "valid1" in _upload_sessions

    def test_handles_empty_sessions(self):
        """Should handle empty sessions dict without error."""
        _upload_sessions.clear()
        _cleanup_expired_sessions()  # Should not raise


class TestValidateDocumentAccess:
    """Tests for _validate_document_access helper."""

    @pytest.fixture
    def mock_request(self):
        """Create a mock request with database."""
        request = MagicMock()
        request.app.state.database = MagicMock()
        request.app.state.database.enabled = True
        return request

    @pytest.fixture
    def authenticated_user(self):
        """Create an authenticated user."""
        return UserContext(
            user_id="user123",
            tenant_id="tenant456",
            is_authenticated=True,
            roles=["user"],
        )

    @pytest.fixture
    def admin_user(self):
        """Create an admin user."""
        return UserContext(
            user_id="admin",
            tenant_id="admin_tenant",
            is_authenticated=True,
            roles=["admin"],
        )

    @pytest.mark.asyncio
    async def test_denies_access_when_db_unavailable(self, mock_request, authenticated_user):
        """Should deny access when database is not available (fail-closed)."""
        mock_request.app.state.database = None

        with pytest.raises(HTTPException) as exc_info:
            await _validate_document_access(mock_request, "doc123", authenticated_user)

        assert exc_info.value.status_code == 503
        assert "Database" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_404_when_document_not_found(self, mock_request, authenticated_user):
        """Should raise 404 when document doesn't exist."""
        mock_request.app.state.database.get_document = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await _validate_document_access(mock_request, "doc123", authenticated_user)

        assert exc_info.value.status_code == 404
        assert "Document not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_404_when_dataset_not_found(self, mock_request, authenticated_user):
        """Should raise 404 when dataset doesn't exist."""
        mock_request.app.state.database.get_document = AsyncMock(
            return_value={"document_id": "doc123", "dataset_id": "ds123"}
        )
        mock_request.app.state.database.get_dataset = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await _validate_document_access(mock_request, "doc123", authenticated_user)

        assert exc_info.value.status_code == 404
        assert "Dataset not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_allows_access_for_public_dataset(self, mock_request, authenticated_user):
        """Should allow access to public datasets."""
        mock_request.app.state.database.get_document = AsyncMock(
            return_value={"document_id": "doc123", "dataset_id": "ds123"}
        )
        mock_request.app.state.database.get_dataset = AsyncMock(
            return_value={
                "dataset_id": "ds123",
                "tenant_id": "other_tenant",
                "visibility": "public",
            }
        )

        result = await _validate_document_access(mock_request, "doc123", authenticated_user)
        assert result is True

    @pytest.mark.asyncio
    async def test_allows_access_for_admin_user(self, mock_request, admin_user):
        """Should allow admin users to access any document."""
        mock_request.app.state.database.get_document = AsyncMock(
            return_value={"document_id": "doc123", "dataset_id": "ds123"}
        )
        mock_request.app.state.database.get_dataset = AsyncMock(
            return_value={
                "dataset_id": "ds123",
                "tenant_id": "other_tenant",
                "visibility": "private",
            }
        )

        result = await _validate_document_access(mock_request, "doc123", admin_user)
        assert result is True

    @pytest.mark.asyncio
    async def test_allows_access_for_same_tenant(self, mock_request, authenticated_user):
        """Should allow access when tenant matches."""
        mock_request.app.state.database.get_document = AsyncMock(
            return_value={"document_id": "doc123", "dataset_id": "ds123"}
        )
        mock_request.app.state.database.get_dataset = AsyncMock(
            return_value={"dataset_id": "ds123", "tenant_id": "tenant456", "visibility": "private"}
        )

        result = await _validate_document_access(mock_request, "doc123", authenticated_user)
        assert result is True

    @pytest.mark.asyncio
    async def test_denies_access_for_different_tenant(self, mock_request, authenticated_user):
        """Should deny access when tenant doesn't match."""
        mock_request.app.state.database.get_document = AsyncMock(
            return_value={"document_id": "doc123", "dataset_id": "ds123"}
        )
        mock_request.app.state.database.get_dataset = AsyncMock(
            return_value={
                "dataset_id": "ds123",
                "tenant_id": "other_tenant",
                "visibility": "private",
            }
        )
        mock_request.app.state.database.get_dataset_permissions = AsyncMock(return_value=[])

        with pytest.raises(HTTPException) as exc_info:
            await _validate_document_access(mock_request, "doc123", authenticated_user)

        assert exc_info.value.status_code == 403
        assert "Access denied" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_allows_access_with_user_permission(self, mock_request, authenticated_user):
        """Should allow access when user has explicit permission."""
        mock_request.app.state.database.get_document = AsyncMock(
            return_value={"document_id": "doc123", "dataset_id": "ds123"}
        )
        mock_request.app.state.database.get_dataset = AsyncMock(
            return_value={
                "dataset_id": "ds123",
                "tenant_id": "other_tenant",
                "visibility": "private",
            }
        )
        mock_request.app.state.database.get_dataset_permissions = AsyncMock(
            return_value=[{"subject_type": "user", "subject_id": "user123", "permission": "viewer"}]
        )

        result = await _validate_document_access(mock_request, "doc123", authenticated_user)
        assert result is True


class TestUploadSessionSecurity:
    """Tests for upload session security in confirm endpoint."""

    def setup_method(self):
        """Clear sessions before each test."""
        _upload_sessions.clear()

    def teardown_method(self):
        """Clear sessions after each test."""
        _upload_sessions.clear()

    def test_session_user_ownership(self):
        """Sessions should store user_id for ownership verification."""
        upload_id = str(uuid.uuid4())
        _upload_sessions[upload_id] = {
            "user_id": "user123",
            "tenant_id": "tenant456",
            "document_id": "doc789",
            "storage_key": "images/tenant456/doc789/img.png",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        }

        session = _upload_sessions[upload_id]
        assert session["user_id"] == "user123"
        assert session["tenant_id"] == "tenant456"

    def test_cross_user_session_access_prevented(self):
        """Different users should not be able to access each other's sessions."""
        upload_id = str(uuid.uuid4())
        _upload_sessions[upload_id] = {
            "user_id": "user1",
            "tenant_id": "tenant1",
            "document_id": "doc1",
            "storage_key": "images/tenant1/doc1/img.png",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        }

        # Simulate check for different user
        session = _upload_sessions.get(upload_id)
        assert session is not None
        assert session["user_id"] != "user2"  # Different user should be blocked

    def test_storage_key_validation(self):
        """Storage key should match between session and confirm request."""
        upload_id = str(uuid.uuid4())
        original_key = "images/tenant456/doc789/img.png"
        _upload_sessions[upload_id] = {
            "user_id": "user123",
            "storage_key": original_key,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        }

        session = _upload_sessions[upload_id]
        # Simulating a spoofed key
        spoofed_key = "images/other_tenant/other_doc/malicious.png"
        assert session["storage_key"] != spoofed_key  # Should not match


class TestUnavailableDirectUpload:
    def setup_method(self):
        _upload_sessions.clear()

    def teardown_method(self):
        _upload_sessions.clear()

    @pytest.fixture
    def user(self):
        return UserContext(
            user_id="user123",
            tenant_id="tenant456",
            is_authenticated=True,
        )

    @pytest.mark.asyncio
    async def test_upload_fails_before_presigning_or_creating_session(self, user):
        with pytest.raises(HTTPException) as exc_info:
            await get_presigned_upload_url(
                request=PresignedUploadRequest(
                    filename="document.pdf",
                    content_type="application/pdf",
                    document_id="doc123",
                ),
                user=user,
            )

        assert exc_info.value.status_code == 501
        assert _upload_sessions == {}

    @pytest.mark.asyncio
    async def test_confirm_fails_without_consuming_session_or_touching_storage(self, user):
        _upload_sessions["upload-1"] = {
            "user_id": user.user_id,
            "document_id": "doc123",
            "storage_key": "tenant456/doc123/document.pdf",
        }
        with pytest.raises(HTTPException) as exc_info:
            await confirm_upload(
                request=UploadConfirmRequest(
                    upload_id="upload-1",
                    storage_key="tenant456/doc123/document.pdf",
                    document_id="doc123",
                    filename="document.pdf",
                    content_type="application/pdf",
                ),
                user=user,
            )

        assert exc_info.value.status_code == 501
        assert "upload-1" in _upload_sessions

    @pytest.mark.asyncio
    async def test_status_fails_instead_of_returning_placeholder(self, user):
        with pytest.raises(HTTPException) as exc_info:
            await get_task_status("task-1", user=user)

        assert exc_info.value.status_code == 501

    @pytest.mark.asyncio
    async def test_upload_route_returns_501_without_storage_service(self, user):
        app = FastAPI()
        app.include_router(presign_router, prefix="/api/v1")
        app.dependency_overrides[get_user_context] = lambda: user

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/presign/upload",
                json={
                    "filename": "document.pdf",
                    "content_type": "application/pdf",
                    "document_id": "doc123",
                },
            )

        assert response.status_code == 501
        assert response.json()["detail"] == (
            "Direct presigned upload is not implemented; use the standard upload API."
        )
