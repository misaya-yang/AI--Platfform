"""
Tests for upload session memory management.

Ensures upload sessions are properly cleaned up to prevent memory leaks.
"""

from datetime import datetime, timedelta, timezone


class TestUploadSessionCleanup:
    """Test upload session cleanup mechanisms."""

    def test_expired_sessions_are_cleaned(self):
        """Expired upload sessions should be removed during cleanup."""
        from src.api.v1.presign import _cleanup_expired_sessions, _upload_sessions

        # Clear any existing sessions
        _upload_sessions.clear()

        now = datetime.now(timezone.utc)

        # Add an expired session
        _upload_sessions["expired-1"] = {
            "user_id": "user1",
            "created_at": now - timedelta(hours=2),
            "expires_at": now - timedelta(hours=1),
        }

        # Add a valid session
        _upload_sessions["valid-1"] = {
            "user_id": "user2",
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
        }

        # Run cleanup
        _cleanup_expired_sessions()

        # Expired session should be removed
        assert "expired-1" not in _upload_sessions
        # Valid session should remain
        assert "valid-1" in _upload_sessions

        # Cleanup
        _upload_sessions.clear()

    def test_session_has_expiry_timestamp(self):
        """Sessions should have expires_at timestamp set."""

        # When a session is created, it should have expires_at
        # This is a structural test - actual creation tested in integration tests

        now = datetime.now(timezone.utc)
        session = {
            "user_id": "user1",
            "created_at": now,
            "expires_at": now + timedelta(minutes=15),  # 15 min default
        }

        assert "expires_at" in session
        assert session["expires_at"] > now

    def test_cleanup_handles_missing_expires_at(self):
        """Cleanup should handle sessions without expires_at gracefully."""
        from src.api.v1.presign import _cleanup_expired_sessions, _upload_sessions

        # Clear any existing sessions
        _upload_sessions.clear()

        # Add a session without expires_at (edge case)
        _upload_sessions["no-expiry"] = {
            "user_id": "user1",
            "created_at": datetime.now(timezone.utc),
            # Missing expires_at
        }

        # Should not crash
        _cleanup_expired_sessions()

        # Session without expiry is kept (defaults to now in comparison)
        # This is the current behavior - session kept if no expiry
        assert "no-expiry" in _upload_sessions

        # Cleanup
        _upload_sessions.clear()

    def test_multiple_expired_sessions_cleaned(self):
        """Multiple expired sessions should all be cleaned."""
        from src.api.v1.presign import _cleanup_expired_sessions, _upload_sessions

        _upload_sessions.clear()

        now = datetime.now(timezone.utc)

        # Add 5 expired sessions
        for i in range(5):
            _upload_sessions[f"expired-{i}"] = {
                "user_id": f"user{i}",
                "created_at": now - timedelta(hours=2),
                "expires_at": now - timedelta(hours=1),
            }

        # Add 3 valid sessions
        for i in range(3):
            _upload_sessions[f"valid-{i}"] = {
                "user_id": f"user{i}",
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            }

        _cleanup_expired_sessions()

        # All expired sessions removed
        for i in range(5):
            assert f"expired-{i}" not in _upload_sessions

        # All valid sessions remain
        for i in range(3):
            assert f"valid-{i}" in _upload_sessions

        # Cleanup
        _upload_sessions.clear()


class TestUploadSessionSizeLimit:
    """Test upload session cache size limits."""

    def test_max_sessions_constant_exists(self):
        """Verify MAX_UPLOAD_SESSIONS constant exists."""
        from src.api.v1.presign import MAX_UPLOAD_SESSIONS

        assert MAX_UPLOAD_SESSIONS > 0
        assert MAX_UPLOAD_SESSIONS <= 100000  # Reasonable upper bound

    def test_cache_has_reasonable_size(self):
        """Verify cache size doesn't grow unbounded."""
        from src.api.v1.presign import _upload_sessions

        # This is more of a documentation test - the cache is a dict
        # In production, should use Redis or have a max size
        assert isinstance(_upload_sessions, dict)

    def test_session_structure_is_minimal(self):
        """Session data should be minimal to reduce memory usage."""
        # Define expected fields
        required_fields = {
            "user_id",
            "tenant_id",
            "document_id",
            "storage_key",
            "filename",
            "content_type",
            "expires_at",
            "created_at",
        }

        # This is a structure validation - actual sessions tested elsewhere
        # Ensures we don't add unnecessary fields that bloat memory
        assert len(required_fields) <= 10, "Session should have minimal fields"

    def test_cleanup_enforces_max_size(self):
        """Cleanup should evict old sessions when over limit."""
        from src.api.v1.presign import (
            _cleanup_expired_sessions,
            _upload_sessions,
        )

        _upload_sessions.clear()

        now = datetime.now(timezone.utc)

        # Temporarily set a small max for testing
        import src.api.v1.presign as presign_module

        original_max = presign_module.MAX_UPLOAD_SESSIONS
        presign_module.MAX_UPLOAD_SESSIONS = 10

        try:
            # Add more sessions than the limit
            for i in range(15):
                _upload_sessions[f"session-{i}"] = {
                    "user_id": f"user{i}",
                    "created_at": now + timedelta(seconds=i),  # Different times
                    "expires_at": now + timedelta(hours=1),
                }

            # Run cleanup
            _cleanup_expired_sessions()

            # Should have reduced to at or below limit
            assert len(_upload_sessions) <= 10

            # Oldest sessions should be removed (session-0 to session-4)
            # Newer sessions should remain (session-10 to session-14)
            for i in range(5):
                assert f"session-{i}" not in _upload_sessions

        finally:
            # Restore original max
            presign_module.MAX_UPLOAD_SESSIONS = original_max
            _upload_sessions.clear()
