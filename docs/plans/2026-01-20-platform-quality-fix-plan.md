# AI Gateway 平台质量修复实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 87+ issues identified in the platform quality review, achieving enterprise-grade production readiness.

**Architecture:** Security fixes first (Critical), then functionality completion, code quality improvements, frontend UX enhancements, and finally test coverage expansion.

**Tech Stack:** Python 3.11+, FastAPI, asyncpg, PyJWT, React 18, TypeScript, TanStack Query v5, shadcn/ui

---

## Phase 1: Critical Security Fixes (11 issues)

### Task 1: JWT Signature Verification

**Files:**
- Modify: `src/core/middleware/streaming.py:239-265`
- Create: `tests/core/middleware/test_jwt_verification.py`

**Step 1: Write the failing test**

```python
# tests/core/middleware/test_jwt_verification.py
import pytest
import jwt
from datetime import datetime, timedelta

class TestJWTVerification:
    """Test JWT signature verification in streaming middleware."""

    @pytest.fixture
    def jwt_secret(self):
        return "test-secret-key-minimum-32-chars!"

    @pytest.fixture
    def valid_token(self, jwt_secret):
        payload = {
            "sub": "user123",
            "tenant_id": "tenant1",
            "tier": "premium",
            "roles": ["user"],
            "exp": datetime.utcnow() + timedelta(hours=1)
        }
        return jwt.encode(payload, jwt_secret, algorithm="HS256")

    @pytest.fixture
    def forged_token(self):
        """Token signed with wrong secret - should be rejected."""
        payload = {
            "sub": "attacker",
            "tenant_id": "victim_tenant",
            "tier": "admin",
            "roles": ["admin"],
            "exp": datetime.utcnow() + timedelta(hours=1)
        }
        return jwt.encode(payload, "wrong-secret", algorithm="HS256")

    def test_valid_jwt_is_accepted(self, jwt_secret, valid_token):
        """Valid JWT with correct signature should authenticate user."""
        from src.core.middleware.streaming import StreamingRateLimitMiddleware

        middleware = StreamingRateLimitMiddleware(
            app=None,
            config=type('Config', (), {'jwt_secret': jwt_secret, 'jwt_algorithms': ['HS256']})()
        )

        scope = {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {valid_token}".encode())],
            "client": ("127.0.0.1", 12345),
        }

        user_info = middleware._extract_user_info(scope)

        assert user_info["is_authenticated"] is True
        assert user_info["user_id"] == "user123"
        assert user_info["tenant_id"] == "tenant1"

    def test_forged_jwt_is_rejected(self, jwt_secret, forged_token):
        """Forged JWT with wrong signature should NOT authenticate."""
        from src.core.middleware.streaming import StreamingRateLimitMiddleware

        middleware = StreamingRateLimitMiddleware(
            app=None,
            config=type('Config', (), {'jwt_secret': jwt_secret, 'jwt_algorithms': ['HS256']})()
        )

        scope = {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {forged_token}".encode())],
            "client": ("127.0.0.1", 12345),
        }

        user_info = middleware._extract_user_info(scope)

        # CRITICAL: Forged token must NOT be authenticated
        assert user_info["is_authenticated"] is False

    def test_expired_jwt_is_rejected(self, jwt_secret):
        """Expired JWT should not authenticate."""
        expired_payload = {
            "sub": "user123",
            "exp": datetime.utcnow() - timedelta(hours=1)
        }
        expired_token = jwt.encode(expired_payload, jwt_secret, algorithm="HS256")

        from src.core.middleware.streaming import StreamingRateLimitMiddleware

        middleware = StreamingRateLimitMiddleware(
            app=None,
            config=type('Config', (), {'jwt_secret': jwt_secret, 'jwt_algorithms': ['HS256']})()
        )

        scope = {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {expired_token}".encode())],
            "client": ("127.0.0.1", 12345),
        }

        user_info = middleware._extract_user_info(scope)
        assert user_info["is_authenticated"] is False
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/core/middleware/test_jwt_verification.py -v`
Expected: FAIL - `test_forged_jwt_is_rejected` passes (because current code doesn't verify!)

**Step 3: Write minimal implementation**

```python
# src/core/middleware/streaming.py - Replace lines 239-265

def _extract_user_info(self, scope: Scope) -> Dict[str, Any]:
    """Extract and VERIFY user info from request headers.

    CRITICAL: JWT signature must be verified before trusting claims.
    """
    headers = dict(scope.get("headers", []))
    client_ip = self._get_client_ip(scope)

    # Try JWT with VERIFICATION
    auth_header = headers.get(b"authorization", b"").decode()
    if auth_header.lower().startswith("bearer "):
        try:
            import jwt
            token = auth_header.split(" ", 1)[1]

            # CRITICAL FIX: Verify signature using configured secret
            jwt_secret = getattr(self.config, 'jwt_secret', None)
            jwt_algorithms = getattr(self.config, 'jwt_algorithms', ['HS256'])

            if not jwt_secret:
                logger.warning("JWT secret not configured, skipping JWT auth")
            else:
                payload = jwt.decode(
                    token,
                    key=jwt_secret,
                    algorithms=jwt_algorithms,
                    options={"verify_signature": True, "verify_exp": True}
                )

                user_id = str(payload.get("sub") or payload.get("user_id") or "")
                if user_id:
                    return {
                        "user_id": user_id,
                        "user_type": "user",
                        "tenant_id": str(payload.get("tenant_id", "")),
                        "tier": str(payload.get("tier", "normal")),
                        "is_authenticated": True,
                        "ip": client_ip,
                        "roles": payload.get("roles", ["user"]),
                    }
        except jwt.InvalidSignatureError:
            logger.warning(f"Invalid JWT signature from {client_ip}")
        except jwt.ExpiredSignatureError:
            logger.warning(f"Expired JWT from {client_ip}")
        except jwt.DecodeError as e:
            logger.warning(f"JWT decode failed from {client_ip}: {e}")
        except Exception as e:
            logger.error(f"Unexpected JWT error from {client_ip}: {e}")

    # Continue with API key authentication...
    # (rest of existing code for API key auth)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/core/middleware/test_jwt_verification.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/core/middleware/test_jwt_verification.py src/core/middleware/streaming.py
git commit -m "$(cat <<'EOF'
fix(security): add JWT signature verification in streaming middleware

CRITICAL SECURITY FIX: Previously JWT tokens were decoded without
signature verification, allowing attackers to forge authentication.

Now properly validates JWT signature, expiration, and claims using
PyJWT library with configurable secret and algorithms.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Path Traversal Protection Enhancement

**Files:**
- Modify: `src/api/v1/files.py:57-61, 210-216`
- Create: `tests/api/test_path_traversal.py`

**Step 1: Write the failing test**

```python
# tests/api/test_path_traversal.py
import pytest
from pathlib import Path

class TestPathTraversalProtection:
    """Test path traversal attack prevention."""

    def test_dotdot_in_user_id_rejected(self):
        """User ID containing .. should be rejected."""
        from src.api.v1.files import validate_user_id
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_user_id("../../../etc/passwd")

        assert exc_info.value.status_code == 400

    def test_slash_in_user_id_rejected(self):
        """User ID containing / should be rejected."""
        from src.api.v1.files import validate_user_id
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_user_id("user/subdir")

        assert exc_info.value.status_code == 400

    def test_backslash_in_user_id_rejected(self):
        """User ID containing \\ should be rejected."""
        from src.api.v1.files import validate_user_id
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_user_id("user\\subdir")

        assert exc_info.value.status_code == 400

    def test_path_escape_detection(self, tmp_path):
        """Resolved path escaping base directory should be rejected."""
        from src.api.v1.files import get_user_uploads_path, validate_user_id
        from fastapi import HTTPException
        import os

        # Set up temp uploads directory
        os.environ["UPLOADS_PATH"] = str(tmp_path)

        # Valid user should work
        valid_path = get_user_uploads_path("validuser123")
        assert tmp_path in valid_path.parents or valid_path.parent == tmp_path

    def test_valid_user_id_accepted(self):
        """Valid user IDs should pass validation."""
        from src.api.v1.files import validate_user_id

        # These should all pass
        assert validate_user_id("user123") == "user123"
        assert validate_user_id("user_name") == "user_name"
        assert validate_user_id("user-name-123") == "user-name-123"
        assert validate_user_id("A" * 64) == "A" * 64  # Max length
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_path_traversal.py -v`
Expected: Some tests may pass (existing validation), but escape detection test likely fails

**Step 3: Write minimal implementation**

```python
# src/api/v1/files.py - Enhance validate_user_id and get_user_uploads_path

def validate_user_id(user_id: str) -> str:
    """Validate user_id format to prevent path traversal attacks."""
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required")

    # Check pattern (alphanumeric, underscore, hyphen only)
    if not USER_ID_PATTERN.match(user_id):
        logger.warning(f"[Security] Invalid user_id format attempted: {user_id[:50]}")
        raise HTTPException(
            status_code=400,
            detail="Invalid user ID format. Only alphanumeric, underscore, and hyphen allowed (max 64 chars)"
        )

    # ENHANCEMENT: Explicit path traversal pattern rejection
    if ".." in user_id or "/" in user_id or "\\" in user_id:
        logger.warning(f"[Security] Path traversal attempt in user_id: {user_id[:50]}")
        raise HTTPException(status_code=400, detail="Invalid user ID")

    return user_id


def get_user_uploads_path(user_id: str) -> Path:
    """Get user-specific uploads directory with validation."""
    validated_id = validate_user_id(user_id)
    base_path = get_uploads_path()
    path = base_path / validated_id

    # ENHANCEMENT: Verify final path doesn't escape uploads directory
    try:
        resolved = path.resolve()
        base_resolved = base_path.resolve()
        # Ensure path is within base directory
        resolved.relative_to(base_resolved)
    except ValueError:
        logger.error(f"[Security] Path escape attempt detected for user {user_id[:50]}")
        raise HTTPException(status_code=400, detail="Invalid path")

    path.mkdir(parents=True, exist_ok=True)
    return path
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_path_traversal.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/api/test_path_traversal.py src/api/v1/files.py
git commit -m "$(cat <<'EOF'
fix(security): enhance path traversal protection in file uploads

Added explicit checks for path traversal patterns (.., /, \\) and
path escape detection using resolve().relative_to() verification.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Health Endpoint Authentication

**Files:**
- Modify: `src/api/v1/health.py:14-122`
- Create: `tests/api/test_health_auth.py`

**Step 1: Write the failing test**

```python
# tests/api/test_health_auth.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

class TestHealthEndpointAuth:
    """Test health endpoint authentication requirements."""

    @pytest.fixture
    def client(self):
        from src.main import create_app
        app = create_app()
        return TestClient(app)

    def test_basic_health_is_public(self, client):
        """Basic /health endpoint should be public but minimal."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        # Should only return status, NOT service count
        assert "status" in data
        assert "services" not in data  # No info disclosure

    def test_services_health_requires_auth(self, client):
        """Detailed /health/services requires authentication."""
        response = client.get("/api/v1/health/services")
        assert response.status_code == 401

    def test_services_health_requires_admin(self, client):
        """Detailed /health/services requires admin role."""
        # Mock non-admin user
        with patch("src.api.v1.health.get_user_context") as mock_user:
            mock_user.return_value = MagicMock(
                is_authenticated=True,
                user_id="user1",
                roles=["user"]  # Not admin
            )
            response = client.get("/api/v1/health/services")
            assert response.status_code == 403

    def test_providers_health_requires_admin(self, client):
        """Provider health endpoint requires admin."""
        response = client.get("/api/v1/health/providers")
        assert response.status_code == 401
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_health_auth.py -v`
Expected: FAIL - endpoints currently don't require auth

**Step 3: Write minimal implementation**

```python
# src/api/v1/health.py - Add authentication requirements

from fastapi import Depends, HTTPException
from ..deps import get_user_context
from ...core.auth.user_resolver import UserContext


def require_admin(user: UserContext = Depends(get_user_context)) -> UserContext:
    """Require authenticated admin user."""
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    if "admin" not in (user.roles or []):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/health")
async def gateway_health(
    registry: ServiceRegistry = Depends(get_registry),
):
    """Public health check - minimal info only."""
    # Don't expose service count - just status
    return {"status": "ok"}


@router.get("/health/services")
async def all_services_health(
    request: Request,
    monitor: HealthMonitor = Depends(get_health_monitor),
    user: UserContext = Depends(require_admin),  # REQUIRE ADMIN
):
    """All services health - admin only."""
    health_status = {
        service_id: {
            "status": s.status,
            "latency": s.latency,
            "last_check": s.last_check,
            "error": s.error,
        }
        for service_id, s in monitor.all_status().items()
    }
    return health_status


@router.get("/health/services/{service_id}")
async def service_health(
    service_id: str,
    request: Request,
    monitor: HealthMonitor = Depends(get_health_monitor),
    user: UserContext = Depends(require_admin),  # REQUIRE ADMIN
):
    """Single service health - admin only."""
    status = monitor.status(service_id)
    if not status:
        raise HTTPException(status_code=404, detail="Service not found")
    return {
        "service_id": service_id,
        "status": status.status,
        "latency": status.latency,
        "last_check": status.last_check,
        "error": status.error,
    }


@router.get("/health/providers")
async def all_providers_health(
    request: Request,
    user: UserContext = Depends(require_admin),  # REQUIRE ADMIN
):
    """Provider health - admin only."""
    # ... rest of implementation
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_health_auth.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/api/test_health_auth.py src/api/v1/health.py
git commit -m "$(cat <<'EOF'
fix(security): add authentication to health endpoints

Detailed health information now requires admin authentication to
prevent infrastructure fingerprinting and information disclosure.

Basic /health endpoint remains public but only returns status.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Replace Broad Exception Catching

**Files:**
- Modify: `src/core/middleware/streaming.py:264`
- Create: `tests/core/middleware/test_exception_handling.py`

**Step 1: Write the failing test**

```python
# tests/core/middleware/test_exception_handling.py
import pytest
import logging

class TestExceptionHandling:
    """Test proper exception handling (no silent swallowing)."""

    def test_invalid_jwt_is_logged(self, caplog):
        """Invalid JWT should log warning, not silently fail."""
        from src.core.middleware.streaming import StreamingRateLimitMiddleware

        middleware = StreamingRateLimitMiddleware(
            app=None,
            config=type('Config', (), {'jwt_secret': 'secret', 'jwt_algorithms': ['HS256']})()
        )

        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer invalid.token.here")],
            "client": ("127.0.0.1", 12345),
        }

        with caplog.at_level(logging.WARNING):
            user_info = middleware._extract_user_info(scope)

        # Should NOT be authenticated
        assert user_info["is_authenticated"] is False
        # Should have logged a warning
        assert any("JWT" in record.message for record in caplog.records)

    def test_malformed_base64_is_logged(self, caplog):
        """Malformed base64 in JWT should log, not silently pass."""
        from src.core.middleware.streaming import StreamingRateLimitMiddleware

        middleware = StreamingRateLimitMiddleware(
            app=None,
            config=type('Config', (), {'jwt_secret': 'secret', 'jwt_algorithms': ['HS256']})()
        )

        # JWT with invalid base64
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer !!!.invalid.base64!!!")],
            "client": ("127.0.0.1", 12345),
        }

        with caplog.at_level(logging.WARNING):
            user_info = middleware._extract_user_info(scope)

        assert user_info["is_authenticated"] is False
        # Should log the error, not swallow it
        assert len(caplog.records) > 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/core/middleware/test_exception_handling.py -v`
Expected: FAIL - current code uses `except Exception: pass`

**Step 3: Write minimal implementation**

Already included in Task 1 implementation (specific exception types with logging)

**Step 4: Run test to verify it passes**

Run: `pytest tests/core/middleware/test_exception_handling.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/core/middleware/test_exception_handling.py
git commit -m "$(cat <<'EOF'
test(middleware): add tests for exception handling

Verify that JWT errors are properly logged instead of silently swallowed.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Secure Default Configuration

**Files:**
- Modify: `src/config/settings.py:12-16`
- Create: `tests/config/test_settings_security.py`

**Step 1: Write the failing test**

```python
# tests/config/test_settings_security.py
import pytest
import os

class TestSettingsSecurity:
    """Test configuration security defaults."""

    def test_dsn_not_default_credentials(self):
        """DSN should not have default postgres:postgres credentials."""
        from src.config.settings import DatabaseSettings

        settings = DatabaseSettings()
        # DSN should be empty or require explicit configuration
        assert settings.dsn == "" or "postgres:postgres" not in settings.dsn

    def test_auto_init_disabled_by_default(self):
        """auto_init should be False by default for safety."""
        from src.config.settings import DatabaseSettings

        settings = DatabaseSettings()
        assert settings.auto_init is False

    def test_encryption_key_required_in_production(self):
        """Production should require encryption key."""
        os.environ["ENVIRONMENT"] = "production"
        os.environ.pop("GATEWAY_ENCRYPTION_KEY", None)

        from src.config.settings import Settings

        with pytest.raises(ValueError, match="GATEWAY_ENCRYPTION_KEY"):
            Settings()

        os.environ.pop("ENVIRONMENT", None)

    def test_encryption_key_minimum_length(self):
        """Encryption key must be at least 32 characters."""
        os.environ["GATEWAY_ENCRYPTION_KEY"] = "short"

        from src.config.settings import validate_encryption_key

        with pytest.raises(ValueError, match="32 characters"):
            validate_encryption_key("short")

        os.environ.pop("GATEWAY_ENCRYPTION_KEY", None)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/config/test_settings_security.py -v`
Expected: FAIL - current defaults are insecure

**Step 3: Write minimal implementation**

```python
# src/config/settings.py - Fix security defaults

class DatabaseSettings(BaseModel):
    """PostgreSQL 数据库配置"""
    enabled: bool = False
    dsn: str = ""  # CHANGED: Must be explicitly configured
    auto_init: bool = False  # CHANGED: Disabled by default
    permission_cache_ttl_seconds: int = 30
    pool_min_size: int = 2
    pool_max_size: int = 10


def validate_encryption_key(key: str) -> str:
    """Validate encryption key meets security requirements."""
    if not key:
        import os
        if os.getenv("ENVIRONMENT", "").lower() == "production":
            raise ValueError("GATEWAY_ENCRYPTION_KEY is required in production")
        return key

    if len(key) < 32:
        raise ValueError("GATEWAY_ENCRYPTION_KEY must be at least 32 characters")

    return key
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/config/test_settings_security.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/config/test_settings_security.py src/config/settings.py
git commit -m "$(cat <<'EOF'
fix(security): secure configuration defaults

- DSN no longer has default credentials
- auto_init disabled by default
- Encryption key validation with minimum length

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Upload Session Memory Leak Fix

**Files:**
- Modify: `src/api/v1/presign.py:44`
- Create: `tests/api/test_upload_session_cleanup.py`

**Step 1: Write the failing test**

```python
# tests/api/test_upload_session_cleanup.py
import pytest
import asyncio
from datetime import datetime, timedelta

class TestUploadSessionCleanup:
    """Test upload session memory management."""

    @pytest.mark.asyncio
    async def test_expired_sessions_are_cleaned(self):
        """Expired upload sessions should be automatically cleaned."""
        from src.api.v1.presign import _upload_sessions, cleanup_expired_sessions

        # Add an expired session
        _upload_sessions["expired-1"] = {
            "user_id": "user1",
            "created_at": datetime.utcnow() - timedelta(hours=2),
            "expires_at": datetime.utcnow() - timedelta(hours=1),
        }

        # Add a valid session
        _upload_sessions["valid-1"] = {
            "user_id": "user2",
            "created_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(hours=1),
        }

        await cleanup_expired_sessions()

        assert "expired-1" not in _upload_sessions
        assert "valid-1" in _upload_sessions

    def test_session_has_expiry(self):
        """New sessions should have expires_at timestamp."""
        # Session creation should include expiry
        from src.api.v1.presign import SESSION_TTL_SECONDS

        assert SESSION_TTL_SECONDS > 0
        assert SESSION_TTL_SECONDS <= 3600  # Max 1 hour
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_upload_session_cleanup.py -v`
Expected: FAIL - cleanup function doesn't exist

**Step 3: Write minimal implementation**

```python
# src/api/v1/presign.py - Add session cleanup

from datetime import datetime, timedelta
import asyncio

# Session TTL (1 hour)
SESSION_TTL_SECONDS = 3600

# Upload sessions with expiry
_upload_sessions: Dict[str, dict] = {}
_cleanup_task: Optional[asyncio.Task] = None


async def cleanup_expired_sessions():
    """Remove expired upload sessions to prevent memory leak."""
    now = datetime.utcnow()
    expired_keys = [
        key for key, session in _upload_sessions.items()
        if session.get("expires_at", now) < now
    ]
    for key in expired_keys:
        del _upload_sessions[key]

    if expired_keys:
        logger.info(f"Cleaned up {len(expired_keys)} expired upload sessions")


async def start_cleanup_loop():
    """Background task to periodically clean expired sessions."""
    global _cleanup_task
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        await cleanup_expired_sessions()


def create_upload_session(upload_id: str, data: dict) -> None:
    """Create upload session with expiry."""
    data["created_at"] = datetime.utcnow()
    data["expires_at"] = datetime.utcnow() + timedelta(seconds=SESSION_TTL_SECONDS)
    _upload_sessions[upload_id] = data
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_upload_session_cleanup.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/api/test_upload_session_cleanup.py src/api/v1/presign.py
git commit -m "$(cat <<'EOF'
fix(memory): add upload session expiry and cleanup

Prevents memory leak from accumulating upload sessions by:
- Adding expires_at timestamp to each session
- Background cleanup task every 5 minutes
- Session TTL of 1 hour

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2: Important Backend Fixes (24 issues)

### Task 7: SQL Parameterization Fix

**Files:**
- Modify: `src/persistence/database.py:385-407`
- Create: `tests/persistence/test_sql_safety.py`

**Step 1: Write the failing test**

```python
# tests/persistence/test_sql_safety.py
import pytest

class TestSQLSafety:
    """Test SQL query construction safety."""

    def test_parameter_indices_are_correct(self):
        """SQL parameter indices should match params list."""
        # Build query with multiple conditions
        query = "SELECT * FROM services WHERE 1=1"
        params = []

        status = "active"
        service_type = "llm"

        # Simulate current (potentially buggy) approach
        from src.persistence.database import build_service_query

        query, params = build_service_query(
            status=status,
            service_type=service_type,
            tags=["ai", "ml"]
        )

        # Count $N placeholders
        import re
        placeholders = re.findall(r'\$(\d+)', query)
        placeholder_nums = [int(p) for p in placeholders]

        # Verify sequential and match params count
        assert placeholder_nums == list(range(1, len(params) + 1))
        assert len(params) == len(placeholders)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/persistence/test_sql_safety.py -v`
Expected: May pass or fail depending on current implementation

**Step 3: Write minimal implementation**

```python
# src/persistence/database.py - Safe query builder

def build_service_query(
    status: Optional[str] = None,
    service_type: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Tuple[str, List[Any]]:
    """Build service query with safe parameterization."""
    query_parts = ["SELECT id, name, status, service_type, tags, created_at FROM services WHERE 1=1"]
    params: List[Any] = []

    if status:
        params.append(status)
        query_parts.append(f"AND status = ${len(params)}")

    if service_type:
        params.append(service_type)
        query_parts.append(f"AND service_type = ${len(params)}")

    if tags:
        params.append(tags)
        query_parts.append(f"AND tags && ${len(params)}")

    query_parts.append("ORDER BY created_at DESC")

    return " ".join(query_parts), params
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/persistence/test_sql_safety.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/persistence/test_sql_safety.py src/persistence/database.py
git commit -m "$(cat <<'EOF'
fix(database): safe SQL parameterization with len(params) indices

Replaced manual param_idx tracking with len(params) for safer,
less error-prone parameter indexing.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Permission Cache Size Limit

**Files:**
- Modify: `src/persistence/database.py:48-72`
- Create: `tests/persistence/test_permission_cache.py`

**Step 1: Write the failing test**

```python
# tests/persistence/test_permission_cache.py
import pytest
import asyncio

class TestPermissionCache:
    """Test permission cache memory safety."""

    @pytest.mark.asyncio
    async def test_cache_has_size_limit(self):
        """Cache should not grow unbounded."""
        from src.persistence.database import DatabaseStorage

        db = DatabaseStorage(dsn="", enabled=False)

        # Try to add more entries than limit
        max_size = db._permission_cache_max_size

        for i in range(max_size + 100):
            await db._set_cached_permissions(f"user_{i}", ["read"])

        # Cache should be bounded
        assert len(db._permission_cache) <= max_size

    @pytest.mark.asyncio
    async def test_cache_evicts_oldest(self):
        """When cache is full, oldest entries should be evicted."""
        from src.persistence.database import DatabaseStorage

        db = DatabaseStorage(dsn="", enabled=False)
        db._permission_cache_max_size = 3

        await db._set_cached_permissions("user_1", ["read"])
        await db._set_cached_permissions("user_2", ["write"])
        await db._set_cached_permissions("user_3", ["admin"])
        await db._set_cached_permissions("user_4", ["new"])

        # user_1 should be evicted (oldest)
        assert "user_1" not in db._permission_cache
        assert "user_4" in db._permission_cache
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/persistence/test_permission_cache.py -v`
Expected: FAIL - no size limit exists

**Step 3: Write minimal implementation**

```python
# src/persistence/database.py - Add cache size limit

def __init__(self, ...):
    # ... existing init ...
    self._permission_cache: Dict[str, tuple[List[str], float]] = {}
    self._permission_cache_max_size = 10000  # ADD THIS
    self._permission_cache_ttl_seconds = max(int(permission_cache_ttl_seconds or 0), 0)
    self._permission_cache_lock = asyncio.Lock()


async def _set_cached_permissions(self, user_id: str, permissions: List[str]) -> None:
    """Set cached permissions with size limit enforcement."""
    if self._permission_cache_ttl_seconds <= 0:
        return

    async with self._permission_cache_lock:
        # Enforce size limit (simple FIFO eviction)
        while len(self._permission_cache) >= self._permission_cache_max_size:
            if self._permission_cache:
                oldest_key = next(iter(self._permission_cache))
                del self._permission_cache[oldest_key]

        self._permission_cache[user_id] = (
            list(permissions),
            time.time() + self._permission_cache_ttl_seconds,
        )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/persistence/test_permission_cache.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/persistence/test_permission_cache.py src/persistence/database.py
git commit -m "$(cat <<'EOF'
fix(memory): add permission cache size limit

Prevents unbounded memory growth by limiting cache to 10000 entries
with FIFO eviction when full.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3: Frontend UX Fixes (12 issues)

### Task 9: Form Validation Error Display

**Files:**
- Modify: `web/src/components/llm/ModelForm.tsx:73-132`
- Create: `web/src/components/llm/__tests__/ModelForm.test.tsx`

**Step 1: Write the failing test**

```tsx
// web/src/components/llm/__tests__/ModelForm.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ModelForm } from "../ModelForm";

describe("ModelForm", () => {
  it("shows validation error for negative context_window", async () => {
    render(<ModelForm providers={[]} onSubmit={jest.fn()} />);

    const contextInput = screen.getByLabelText(/context.*window/i);
    fireEvent.change(contextInput, { target: { value: "-1" } });
    fireEvent.blur(contextInput);

    await waitFor(() => {
      expect(screen.getByText(/must be positive/i)).toBeInTheDocument();
    });
  });

  it("shows validation error for negative price", async () => {
    render(<ModelForm providers={[]} onSubmit={jest.fn()} />);

    const priceInput = screen.getByLabelText(/input.*price/i);
    fireEvent.change(priceInput, { target: { value: "-0.5" } });
    fireEvent.blur(priceInput);

    await waitFor(() => {
      expect(screen.getByText(/cannot be negative/i)).toBeInTheDocument();
    });
  });

  it("shows required field indicator for model_id", () => {
    render(<ModelForm providers={[]} onSubmit={jest.fn()} />);

    const modelIdLabel = screen.getByText(/model.*id/i);
    expect(modelIdLabel.parentElement).toHaveTextContent("*");
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd web && npm test -- ModelForm.test.tsx`
Expected: FAIL - validation not implemented

**Step 3: Write minimal implementation**

```tsx
// web/src/components/llm/ModelForm.tsx - Add validation

const {
  register,
  handleSubmit,
  reset,
  watch,
  setValue,
  formState: { errors },
} = useForm<FormData>({
  defaultValues: { /* ... */ },
  mode: "onBlur",  // Validate on blur
});

// In form fields:
<FormItem>
  <FormLabel>
    {t("models.form.contextWindow")} <span className="text-red-500">*</span>
  </FormLabel>
  <Input
    type="number"
    {...register("context_window", {
      required: t("validation.required"),
      min: { value: 1, message: t("validation.mustBePositive") },
      max: { value: 10000000, message: t("validation.tooLarge") },
    })}
  />
  {errors.context_window && (
    <p className="text-sm text-red-500">{errors.context_window.message}</p>
  )}
</FormItem>

<FormItem>
  <FormLabel>{t("models.form.inputPrice")}</FormLabel>
  <Input
    type="number"
    step="0.000001"
    {...register("input_price_per_1k", {
      min: { value: 0, message: t("validation.cannotBeNegative") },
    })}
  />
  {errors.input_price_per_1k && (
    <p className="text-sm text-red-500">{errors.input_price_per_1k.message}</p>
  )}
</FormItem>
```

**Step 4: Run test to verify it passes**

Run: `cd web && npm test -- ModelForm.test.tsx`
Expected: PASS

**Step 5: Commit**

```bash
git add web/src/components/llm/ModelForm.tsx web/src/components/llm/__tests__/ModelForm.test.tsx
git commit -m "$(cat <<'EOF'
feat(web): add form validation with error display in ModelForm

- Numeric fields validate min/max values
- Price fields cannot be negative
- Required fields show asterisk
- Validation errors display below fields

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Replace Hardcoded Chinese with i18n

**Files:**
- Modify: `web/src/pages/Services.tsx:84-139, 230-280`
- Modify: `web/src/i18n/locales/en-US.json`
- Modify: `web/src/i18n/locales/zh-CN.json`

**Step 1: Write the failing test**

```tsx
// web/src/pages/__tests__/Services.i18n.test.tsx
import { render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import Services from "../Services";

describe("Services i18n", () => {
  beforeEach(() => {
    i18n.changeLanguage("en-US");
  });

  it("should not contain hardcoded Chinese characters", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <Services />
      </I18nextProvider>
    );

    // Get all text content
    const text = document.body.textContent || "";

    // Check for Chinese characters (CJK Unified Ideographs range)
    const chineseRegex = /[\u4e00-\u9fa5]/;
    expect(chineseRegex.test(text)).toBe(false);
  });

  it("displays English labels when locale is en-US", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <Services />
      </I18nextProvider>
    );

    expect(screen.getByText(/Service Management/i)).toBeInTheDocument();
    expect(screen.getByText(/Add Provider/i)).toBeInTheDocument();
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd web && npm test -- Services.i18n.test.tsx`
Expected: FAIL - hardcoded Chinese exists

**Step 3: Write minimal implementation**

```tsx
// web/src/pages/Services.tsx - Replace hardcoded strings

// Before:
toast({ title: "厂商创建成功" });

// After:
toast({ title: t("services.toast.providerCreated") });

// Before:
<TabsTrigger>服务管理</TabsTrigger>

// After:
<TabsTrigger>{t("services.tabs.serviceManagement")}</TabsTrigger>
```

```json
// web/src/i18n/locales/en-US.json
{
  "services": {
    "tabs": {
      "serviceManagement": "Service Management",
      "providerManagement": "Provider Management"
    },
    "toast": {
      "providerCreated": "Provider created successfully",
      "providerUpdated": "Provider updated successfully",
      "providerDeleted": "Provider deleted successfully",
      "createFailed": "Creation failed",
      "updateFailed": "Update failed",
      "deleteFailed": "Deletion failed"
    },
    "buttons": {
      "addProvider": "Add Provider",
      "addService": "Add Service"
    }
  }
}
```

```json
// web/src/i18n/locales/zh-CN.json
{
  "services": {
    "tabs": {
      "serviceManagement": "服务管理",
      "providerManagement": "厂商管理"
    },
    "toast": {
      "providerCreated": "厂商创建成功",
      "providerUpdated": "厂商更新成功",
      "providerDeleted": "厂商删除成功",
      "createFailed": "创建失败",
      "updateFailed": "更新失败",
      "deleteFailed": "删除失败"
    },
    "buttons": {
      "addProvider": "添加厂商",
      "addService": "添加服务"
    }
  }
}
```

**Step 4: Run test to verify it passes**

Run: `cd web && npm test -- Services.i18n.test.tsx`
Expected: PASS

**Step 5: Commit**

```bash
git add web/src/pages/Services.tsx web/src/i18n/locales/en-US.json web/src/i18n/locales/zh-CN.json
git commit -m "$(cat <<'EOF'
fix(i18n): replace hardcoded Chinese in Services page

All user-visible strings now use t() with proper i18n keys.
Added missing keys to both en-US and zh-CN locales.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Dashboard Context Performance Fix

**Files:**
- Modify: `web/src/pages/dashboard/DashboardContext.tsx:31-61`
- Create: `web/src/pages/dashboard/__tests__/DashboardContext.test.tsx`

**Step 1: Write the failing test**

```tsx
// web/src/pages/dashboard/__tests__/DashboardContext.test.tsx
import { renderHook } from "@testing-library/react";
import { DashboardProvider, useDashboard } from "../DashboardContext";

describe("DashboardContext", () => {
  it("should not create new value object on every render", () => {
    const { result, rerender } = renderHook(() => useDashboard(), {
      wrapper: DashboardProvider,
    });

    const firstValue = result.current;

    rerender();

    const secondValue = result.current;

    // Value should be referentially stable (memoized)
    // If not memoized, this would fail
    expect(Object.is(firstValue, secondValue)).toBe(true);
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd web && npm test -- DashboardContext.test.tsx`
Expected: FAIL - value not memoized

**Step 3: Write minimal implementation**

```tsx
// web/src/pages/dashboard/DashboardContext.tsx

export function DashboardProvider({ children }: { children: React.ReactNode }) {
  // ... existing state ...

  const triggerRefresh = useCallback(() => {
    setLastRefresh(new Date());
  }, []);

  // ... existing useEffect ...

  // MEMOIZE the context value
  const value = useMemo<DashboardContextValue>(
    () => ({
      dateRange,
      setDateRange,
      granularity,
      setGranularity,
      source,
      setSource,
      serviceId,
      setServiceId,
      userId,
      setUserId,
      refreshInterval,
      setRefreshInterval,
      lastRefresh,
      triggerRefresh,
    }),
    [
      dateRange,
      granularity,
      source,
      serviceId,
      userId,
      refreshInterval,
      lastRefresh,
      triggerRefresh,
    ]
  );

  return (
    <DashboardContext.Provider value={value}>
      {children}
    </DashboardContext.Provider>
  );
}
```

**Step 4: Run test to verify it passes**

Run: `cd web && npm test -- DashboardContext.test.tsx`
Expected: PASS

**Step 5: Commit**

```bash
git add web/src/pages/dashboard/DashboardContext.tsx web/src/pages/dashboard/__tests__/DashboardContext.test.tsx
git commit -m "$(cat <<'EOF'
perf(dashboard): memoize context value to reduce re-renders

Context value is now wrapped in useMemo to prevent unnecessary
re-renders of all consuming components.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Table Responsive Design Fix

**Files:**
- Modify: `web/src/pages/Settings.tsx:510-537`

**Step 1: Write the failing test**

```tsx
// web/src/pages/__tests__/Settings.responsive.test.tsx
import { render } from "@testing-library/react";
import Settings from "../Settings";

describe("Settings responsive design", () => {
  it("rate limits table should be scrollable on small screens", () => {
    render(<Settings />);

    const tableContainer = document.querySelector('[data-testid="rate-limits-table"]')?.parentElement;

    expect(tableContainer).toHaveClass("overflow-x-auto");
  });

  it("uses stable keys for table rows", () => {
    const { container } = render(<Settings />);

    const tableRows = container.querySelectorAll("tbody tr");

    tableRows.forEach((row) => {
      const key = row.getAttribute("data-key");
      // Should not be just a number (index)
      expect(key).not.toMatch(/^\d+$/);
    });
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd web && npm test -- Settings.responsive.test.tsx`
Expected: FAIL - no overflow-x-auto class

**Step 3: Write minimal implementation**

```tsx
// web/src/pages/Settings.tsx - Wrap table in scrollable container

<div className="overflow-x-auto">
  <Table data-testid="rate-limits-table">
    <TableHeader>
      <TableRow>
        <TableHead>{t("settings.rateLimit.tableHeaders.scope")}</TableHead>
        <TableHead className="hidden md:table-cell">{t("settings.rateLimit.tableHeaders.scopeId")}</TableHead>
        <TableHead>{t("settings.rateLimit.tableHeaders.requests")}</TableHead>
        <TableHead className="hidden sm:table-cell">{t("settings.rateLimit.tableHeaders.window")}</TableHead>
        <TableHead className="hidden lg:table-cell">{t("settings.rateLimit.tableHeaders.burst")}</TableHead>
        <TableHead>{t("settings.rateLimit.tableHeaders.status")}</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      {rateLimits.map((rule: RateLimitRule) => (
        <TableRow
          key={`${rule.scope}-${rule.scope_id}-${rule.requests}`}
          data-key={`${rule.scope}-${rule.scope_id}-${rule.requests}`}
        >
          {/* columns */}
        </TableRow>
      ))}
    </TableBody>
  </Table>
</div>
```

**Step 4: Run test to verify it passes**

Run: `cd web && npm test -- Settings.responsive.test.tsx`
Expected: PASS

**Step 5: Commit**

```bash
git add web/src/pages/Settings.tsx web/src/pages/__tests__/Settings.responsive.test.tsx
git commit -m "$(cat <<'EOF'
fix(responsive): make Settings tables scrollable on mobile

- Wrapped tables in overflow-x-auto container
- Added responsive column hiding (hidden md:table-cell)
- Fixed table row keys to use composite keys

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4: Test Coverage Expansion

### Task 13: AssistantService Core Tests

**Files:**
- Create: `tests/services/assistant/test_assistant_service.py`

**Step 1: Write the failing test**

```python
# tests/services/assistant/test_assistant_service.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

class TestAssistantService:
    """Tests for AssistantService core functionality."""

    @pytest.fixture
    def mock_database(self):
        db = AsyncMock()
        db.get_session = AsyncMock(return_value={"session_id": "s1", "messages": []})
        db.save_message = AsyncMock()
        return db

    @pytest.fixture
    def mock_model_registry(self):
        registry = MagicMock()
        registry.get_model = MagicMock(return_value={
            "model_id": "gpt-4",
            "provider_id": "openai",
            "context_window": 8192,
        })
        return registry

    @pytest.fixture
    def service(self, mock_database, mock_model_registry):
        from src.services.assistant.assistant_service import AssistantService
        return AssistantService(
            database=mock_database,
            model_registry=mock_model_registry,
        )

    @pytest.mark.asyncio
    async def test_create_session(self, service):
        """Should create a new chat session."""
        session = await service.create_session(
            user_id="user1",
            model_id="gpt-4",
            title="Test Chat",
        )

        assert session is not None
        assert "session_id" in session

    @pytest.mark.asyncio
    async def test_add_message_to_session(self, service, mock_database):
        """Should add message to existing session."""
        result = await service.add_message(
            session_id="s1",
            role="user",
            content="Hello",
        )

        mock_database.save_message.assert_called_once()
        assert result["role"] == "user"

    @pytest.mark.asyncio
    async def test_get_session_with_messages(self, service, mock_database):
        """Should retrieve session with message history."""
        mock_database.get_session.return_value = {
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ]
        }

        session = await service.get_session("s1")

        assert len(session["messages"]) == 2

    @pytest.mark.asyncio
    async def test_session_not_found_raises_error(self, service, mock_database):
        """Should raise error for non-existent session."""
        mock_database.get_session.return_value = None

        with pytest.raises(ValueError, match="Session not found"):
            await service.get_session("nonexistent")
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/services/assistant/test_assistant_service.py -v`
Expected: FAIL or import errors initially

**Step 3: Write minimal implementation**

If AssistantService already exists, verify the tests pass. If not, implement:

```python
# src/services/assistant/assistant_service.py - Ensure these methods exist

async def create_session(self, user_id: str, model_id: str, title: str) -> dict:
    """Create a new chat session."""
    session_id = str(uuid.uuid4())
    # ... implementation
    return {"session_id": session_id, "title": title}

async def get_session(self, session_id: str) -> dict:
    """Get session with messages."""
    session = await self.database.get_session(session_id)
    if not session:
        raise ValueError("Session not found")
    return session

async def add_message(self, session_id: str, role: str, content: str) -> dict:
    """Add message to session."""
    # ... implementation
    return {"role": role, "content": content}
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/services/assistant/test_assistant_service.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/services/assistant/test_assistant_service.py
git commit -m "$(cat <<'EOF'
test(assistant): add core AssistantService tests

Tests for session creation, message handling, and error cases.
Improves test coverage for critical assistant functionality.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Summary

This plan covers:

**Phase 1: Critical Security (Tasks 1-6)**
- JWT signature verification
- Path traversal protection
- Health endpoint authentication
- Exception handling
- Secure defaults
- Memory leak fix

**Phase 2: Backend Quality (Tasks 7-8)**
- SQL parameterization
- Cache size limits

**Phase 3: Frontend UX (Tasks 9-12)**
- Form validation display
- i18n for hardcoded strings
- Context performance
- Responsive tables

**Phase 4: Test Coverage (Task 13+)**
- AssistantService tests
- (Continue with remaining modules)

---

## Execution Notes

1. **Run tests before each task** to verify current state
2. **Commit after each task** - small, focused commits
3. **Request code review** after completing each phase
4. **Use TDD strictly** - write failing test first

Total estimated tasks: 50+ (13 detailed above, rest follow same pattern)

---

*Plan created: 2026-01-20 - AI Gateway Quality Fix Implementation*
