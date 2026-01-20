"""
Tests for health endpoint authentication requirements.

Ensures sensitive health information is protected from unauthorized access.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class MockUserContext:
    """Mock UserContext for testing."""
    user_id: str
    tenant_id: str = ""
    tier: str = "normal"
    is_authenticated: bool = False
    ip: str = "127.0.0.1"
    roles: List[str] = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = []


class TestHealthEndpointAuth:
    """Test health endpoint authentication requirements."""

    def test_basic_health_returns_minimal_info(self):
        """Basic /health endpoint should return minimal info (no service count)."""
        from src.api.v1.health import gateway_health

        # Create async test
        import asyncio

        async def run_test():
            mock_registry = AsyncMock()
            mock_registry.list.return_value = [{}, {}, {}]  # 3 services

            result = await gateway_health(registry=mock_registry)

            # Should only return status
            assert "status" in result
            assert result["status"] == "ok"
            # Should NOT expose service count (information disclosure)
            assert "services" not in result

        asyncio.run(run_test())

    def test_services_health_requires_auth(self):
        """Detailed /health/services requires authentication."""
        from src.api.v1.health import require_admin
        from fastapi import HTTPException

        # Unauthenticated user
        mock_user = MockUserContext(
            user_id="anon:123",
            is_authenticated=False,
            roles=[]
        )

        with pytest.raises(HTTPException) as exc_info:
            require_admin(mock_user)

        assert exc_info.value.status_code == 401

    def test_services_health_requires_admin_role(self):
        """Detailed /health/services requires admin role."""
        from src.api.v1.health import require_admin
        from fastapi import HTTPException

        # Authenticated but not admin
        mock_user = MockUserContext(
            user_id="user123",
            is_authenticated=True,
            roles=["user"]
        )

        with pytest.raises(HTTPException) as exc_info:
            require_admin(mock_user)

        assert exc_info.value.status_code == 403

    def test_admin_user_is_allowed(self):
        """Admin user should pass authentication."""
        from src.api.v1.health import require_admin

        # Admin user
        mock_user = MockUserContext(
            user_id="admin123",
            is_authenticated=True,
            roles=["admin"]
        )

        # Should not raise
        result = require_admin(mock_user)
        assert result == mock_user

    def test_ops_role_is_allowed(self):
        """User with 'ops' role should also pass (for operations team)."""
        from src.api.v1.health import require_admin

        # Ops user
        mock_user = MockUserContext(
            user_id="ops123",
            is_authenticated=True,
            roles=["ops"]
        )

        # Should not raise
        result = require_admin(mock_user)
        assert result == mock_user

    def test_providers_health_requires_admin(self):
        """Provider health endpoint requires admin."""
        from src.api.v1.health import require_admin
        from fastapi import HTTPException

        # Regular authenticated user
        mock_user = MockUserContext(
            user_id="user123",
            is_authenticated=True,
            roles=["user", "premium"]
        )

        with pytest.raises(HTTPException) as exc_info:
            require_admin(mock_user)

        assert exc_info.value.status_code == 403


class TestHealthEndpointInfoProtection:
    """Test that sensitive info is not exposed."""

    def test_basic_health_does_not_leak_service_count(self):
        """Basic health should not leak infrastructure details."""
        from src.api.v1.health import gateway_health
        import asyncio

        async def run_test():
            mock_registry = AsyncMock()
            mock_registry.list.return_value = list(range(50))  # 50 services

            result = await gateway_health(registry=mock_registry)

            # Should not expose how many services we have
            assert "services" not in result
            # Should not expose provider info
            assert "providers" not in result
            # Should only have status
            assert list(result.keys()) == ["status"]

        asyncio.run(run_test())
