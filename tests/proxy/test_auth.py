"""
认证中间件测试

测试内容：
- JWT Token 认证（有效/无效/过期）
- API Key 认证
- Guest Session 认证
- Anonymous 用户处理
- 白名单路径
- 用户信息提取
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from fastapi import HTTPException, Request

from src.core.auth.user_resolver import UserContext
from src.core.middleware.auth import (
    AuthConfig,
    AuthMiddleware,
    RemoteJWTValidator,
    UserInfo,
    default_jwt_decoder,
)
from tests.conftest import (
    TEST_JWT_ALGORITHM,
    TEST_JWT_SECRET,
    create_test_token,
)

# ============ Auth Config Tests ============


class TestAuthConfig:
    """AuthConfig 配置测试"""

    def test_default_config(self):
        """测试默认配置"""
        config = AuthConfig()

        assert config.jwt_enabled is False
        assert config.api_key_enabled is False
        assert config.guest_session_enabled is True
        assert config.anonymous_enabled is True
        assert "HS256" in config.jwt_algorithms

    def test_whitelist_paths_default(self):
        """测试默认白名单路径"""
        config = AuthConfig()

        assert "/health" in config.whitelist_paths
        assert "/health/live" in config.whitelist_paths
        assert "/health/ready" in config.whitelist_paths
        assert "/metrics" not in config.whitelist_paths
        assert "/docs" in config.whitelist_paths

    def test_custom_config(self):
        """测试自定义配置"""
        config = AuthConfig(
            jwt_enabled=True,
            jwt_secret="custom_secret",
            jwt_algorithms=["RS256"],
            api_key_enabled=True,
            api_key_header="X-Custom-Key",
        )

        assert config.jwt_enabled is True
        assert config.jwt_secret == "custom_secret"
        assert "RS256" in config.jwt_algorithms
        assert config.api_key_header == "X-Custom-Key"


# ============ UserInfo Tests ============


class TestUserInfo:
    """UserInfo 测试"""

    def test_user_info_creation(self):
        """测试 UserInfo 创建"""
        user = UserInfo(
            user_id="user_123",
            user_type="user",
            tenant_id="tenant_001",
            tier="premium",
            is_authenticated=True,
            roles=["user", "admin"],
        )

        assert user.user_id == "user_123"
        assert user.user_type == "user"
        assert user.tenant_id == "tenant_001"
        assert user.tier == "premium"
        assert user.is_authenticated is True
        assert "admin" in user.roles

    def test_user_info_to_user_context(self):
        """测试 UserInfo 转换为 UserContext"""
        user = UserInfo(
            user_id="user_123",
            user_type="user",
            tenant_id="tenant_001",
            tier="premium",
            is_authenticated=True,
            roles=["user"],
            ip="192.168.1.1",
        )

        context = user.to_user_context()

        assert isinstance(context, UserContext)
        assert context.user_id == "user_123"
        assert context.tenant_id == "tenant_001"
        assert context.tier == "premium"
        assert context.is_authenticated is True
        assert context.ip == "192.168.1.1"

    def test_anonymous_user_defaults(self):
        """测试匿名用户默认值"""
        user = UserInfo(
            user_id="anon:192.168.1.1",
            user_type="anonymous",
        )

        assert user.tier == "anonymous"
        assert user.is_authenticated is False
        assert user.tenant_id == ""


# ============ JWT Authentication Tests ============


class TestJWTAuthentication:
    """JWT 认证测试"""

    @pytest.fixture
    def jwt_config(self):
        """JWT 配置"""
        return AuthConfig(
            jwt_enabled=True,
            jwt_secret=TEST_JWT_SECRET,
            jwt_algorithms=[TEST_JWT_ALGORITHM],
        )

    def test_valid_jwt_token_decoded(self):
        """测试有效 JWT Token 解码"""
        token = create_test_token(
            user_id="user_123",
            tenant_id="tenant_001",
            tier="premium",
            roles=["user", "admin"],
        )

        payload = jwt.decode(
            token,
            TEST_JWT_SECRET,
            algorithms=[TEST_JWT_ALGORITHM],
        )

        assert payload["user_id"] == "user_123"
        assert payload["tenant_id"] == "tenant_001"
        assert payload["tier"] == "premium"
        assert "admin" in payload["roles"]

    def test_expired_jwt_token_rejected(self):
        """测试过期 JWT Token 被拒绝"""
        expired_token = jwt.encode(
            {
                "user_id": "user_123",
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            TEST_JWT_SECRET,
            algorithm=TEST_JWT_ALGORITHM,
        )

        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(
                expired_token,
                TEST_JWT_SECRET,
                algorithms=[TEST_JWT_ALGORITHM],
            )

    def test_invalid_signature_rejected(self):
        """测试无效签名 Token 被拒绝"""
        token = jwt.encode(
            {"user_id": "user_123"},
            "wrong_secret",
            algorithm=TEST_JWT_ALGORITHM,
        )

        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(
                token,
                TEST_JWT_SECRET,
                algorithms=[TEST_JWT_ALGORITHM],
            )

    def test_malformed_token_rejected(self):
        """测试格式错误 Token 被拒绝"""
        with pytest.raises(jwt.DecodeError):
            jwt.decode(
                "not.a.valid.token",
                TEST_JWT_SECRET,
                algorithms=[TEST_JWT_ALGORITHM],
            )

    @pytest.mark.asyncio
    async def test_default_jwt_decoder(self):
        """测试默认 JWT 解码器"""
        token = create_test_token(
            user_id="user_123",
            tenant_id="tenant_001",
        )

        payload = await default_jwt_decoder(
            token,
            secret=TEST_JWT_SECRET,
            algorithms=[TEST_JWT_ALGORITHM],
        )

        assert payload["user_id"] == "user_123"
        assert payload["tenant_id"] == "tenant_001"

    def test_jwt_with_audience_validation(self):
        """测试带 audience 的 JWT 验证"""
        token = jwt.encode(
            {
                "user_id": "user_123",
                "aud": "api.gateway.local",
            },
            TEST_JWT_SECRET,
            algorithm=TEST_JWT_ALGORITHM,
        )

        payload = jwt.decode(
            token,
            TEST_JWT_SECRET,
            algorithms=[TEST_JWT_ALGORITHM],
            audience="api.gateway.local",
        )

        assert payload["user_id"] == "user_123"

    def test_jwt_with_wrong_audience_rejected(self):
        """测试错误 audience 被拒绝"""
        token = jwt.encode(
            {
                "user_id": "user_123",
                "aud": "wrong.audience",
            },
            TEST_JWT_SECRET,
            algorithm=TEST_JWT_ALGORITHM,
        )

        with pytest.raises(jwt.InvalidAudienceError):
            jwt.decode(
                token,
                TEST_JWT_SECRET,
                algorithms=[TEST_JWT_ALGORITHM],
                audience="api.gateway.local",
            )


# ============ Auth Middleware Tests ============


class TestAuthMiddleware:
    """认证中间件测试"""

    @pytest.fixture
    def mock_request(self):
        """Mock 请求"""
        request = MagicMock(spec=Request)
        request.url.path = "/api/v1/chat"
        request.headers = {}
        request.cookies = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        request.state = MagicMock()
        return request

    @pytest.fixture
    def mock_call_next(self):
        """Mock call_next 函数"""

        async def call_next(request):
            response = MagicMock()
            response.headers = {}
            return response

        return call_next

    @pytest.fixture
    def jwt_decoder(self):
        """JWT 解码器"""

        async def decoder(token, **kwargs):
            return jwt.decode(
                token,
                kwargs.get("secret", TEST_JWT_SECRET),
                algorithms=kwargs.get("algorithms", [TEST_JWT_ALGORITHM]),
            )

        return decoder

    @pytest.mark.asyncio
    async def test_whitelist_path_bypasses_auth(self, mock_request):
        """测试白名单路径跳过认证"""
        mock_request.url.path = "/health"

        config = AuthConfig()
        middleware = AuthMiddleware(
            app=MagicMock(),
            config=config,
        )

        # 白名单路径不需要认证
        assert middleware._is_whitelisted("/health") is True
        assert middleware._is_whitelisted("/metrics") is False
        assert middleware._is_whitelisted("/docs") is True

    @pytest.mark.asyncio
    async def test_non_whitelist_path_requires_auth(self):
        """测试非白名单路径需要认证"""
        config = AuthConfig()
        middleware = AuthMiddleware(
            app=MagicMock(),
            config=config,
        )

        assert middleware._is_whitelisted("/api/v1/chat") is False
        assert middleware._is_whitelisted("/proxy/langgraph") is False

    def test_get_client_ip_from_forwarded(self, mock_request):
        """只有可信代理直连时才信任 X-Forwarded-For。"""
        mock_request.headers = {"X-Forwarded-For": "10.0.0.1, 10.0.0.2"}

        config = AuthConfig()
        middleware = AuthMiddleware(
            app=MagicMock(),
            config=config,
        )

        ip = middleware._get_client_ip(mock_request)
        assert ip == "192.168.1.1"

    def test_get_client_ip_ignores_real_ip_without_trusted_proxy(self, mock_request):
        """非可信直连来源不能用 X-Real-IP 伪造客户端 IP。"""
        mock_request.headers = {"X-Real-IP": "10.0.0.5"}

        config = AuthConfig()
        middleware = AuthMiddleware(
            app=MagicMock(),
            config=config,
        )

        ip = middleware._get_client_ip(mock_request)
        assert ip == "192.168.1.1"

    def test_get_client_ip_fallback(self, mock_request):
        """测试 IP 获取回退到 client.host"""
        mock_request.headers = {}

        config = AuthConfig()
        middleware = AuthMiddleware(
            app=MagicMock(),
            config=config,
        )

        ip = middleware._get_client_ip(mock_request)
        assert ip == "192.168.1.1"


# ============ API Key Authentication Tests ============


class TestAPIKeyAuthentication:
    """API Key 认证测试"""

    @pytest.fixture
    def api_key_validator(self):
        """API Key 验证器"""

        async def validator(api_key):
            valid_keys = {
                "valid_key_001": {
                    "user_id": "api_user_001",
                    "tenant_id": "tenant_001",
                    "tier": "premium",
                    "roles": ["user", "api"],
                },
                "admin_key_001": {
                    "user_id": "admin_user_001",
                    "tenant_id": "tenant_admin",
                    "tier": "admin",
                    "roles": ["admin"],
                },
            }
            return valid_keys.get(api_key)

        return validator

    @pytest.mark.asyncio
    async def test_valid_api_key(self, api_key_validator):
        """测试有效 API Key"""
        result = await api_key_validator("valid_key_001")

        assert result is not None
        assert result["user_id"] == "api_user_001"
        assert result["tier"] == "premium"

    @pytest.mark.asyncio
    async def test_invalid_api_key(self, api_key_validator):
        """测试无效 API Key"""
        result = await api_key_validator("invalid_key")

        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_with_admin_role(self, api_key_validator):
        """测试 Admin API Key"""
        result = await api_key_validator("admin_key_001")

        assert result is not None
        assert "admin" in result["roles"]
        assert result["tier"] == "admin"


# ============ Guest Session Authentication Tests ============


class TestGuestSessionAuthentication:
    """Guest Session 认证测试"""

    @pytest.fixture
    def guest_session_validator(self):
        """Guest Session 验证器"""

        async def validator(session_id):
            valid_sessions = {
                "guest_session_001": {
                    "tenant_id": "public",
                    "created_at": time.time(),
                },
                "expired_session": None,  # 模拟过期会话
            }
            return valid_sessions.get(session_id)

        return validator

    @pytest.mark.asyncio
    async def test_valid_guest_session(self, guest_session_validator):
        """测试有效 Guest Session"""
        result = await guest_session_validator("guest_session_001")

        assert result is not None
        assert result["tenant_id"] == "public"

    @pytest.mark.asyncio
    async def test_expired_guest_session(self, guest_session_validator):
        """测试过期 Guest Session"""
        result = await guest_session_validator("expired_session")

        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_guest_session(self, guest_session_validator):
        """测试未知 Guest Session"""
        result = await guest_session_validator("unknown_session")

        assert result is None


# ============ Anonymous User Tests ============


class TestAnonymousUser:
    """匿名用户测试"""

    def test_anonymous_user_info_created(self):
        """测试匿名用户信息创建"""
        user = UserInfo(
            user_id="anon:192.168.1.1",
            user_type="anonymous",
            tenant_id="public",
            tier="anonymous",
            is_authenticated=False,
            ip="192.168.1.1",
            roles=["guest"],
        )

        assert user.user_type == "anonymous"
        assert user.is_authenticated is False
        assert user.tier == "anonymous"
        assert "guest" in user.roles

    def test_anonymous_user_from_ip(self):
        """测试从 IP 创建匿名用户"""
        client_ip = "10.0.0.100"
        user = UserInfo(
            user_id=f"anon:{client_ip}",
            user_type="anonymous",
            tenant_id="public",
            tier="anonymous",
            is_authenticated=False,
            ip=client_ip,
            roles=["guest"],
        )

        assert user.user_id == "anon:10.0.0.100"
        assert user.ip == client_ip


# ============ Remote JWT Validator Tests ============


class TestRemoteJWTValidator:
    """远程 JWT 验证器测试"""

    @pytest.fixture
    def remote_validator(self):
        """远程验证器实例"""
        return RemoteJWTValidator(
            verify_url="http://auth-service/verify",
            timeout=5.0,
            cache_ttl=60,
        )

    def test_validator_config(self, remote_validator):
        """测试验证器配置"""
        assert remote_validator.verify_url == "http://auth-service/verify"
        assert remote_validator.timeout == 5.0
        assert remote_validator.cache_ttl == 60

    @pytest.mark.asyncio
    async def test_cached_token_reused(self, remote_validator):
        """测试缓存 Token 被复用"""
        # 预填充缓存
        token = "cached_token_001"
        payload = {"user_id": "cached_user", "tenant_id": "tenant_001"}
        remote_validator._cache[token] = (payload, time.time() + 60)

        result = await remote_validator(token)

        assert result["user_id"] == "cached_user"

    @pytest.mark.asyncio
    async def test_expired_cache_ignored(self, remote_validator):
        """测试过期缓存被忽略"""
        token = "expired_cache_token"
        payload = {"user_id": "old_user"}
        # 缓存已过期
        remote_validator._cache[token] = (payload, time.time() - 10)

        # 由于缓存过期，会尝试远程验证
        # 这里会失败因为没有真实的远程服务
        with pytest.raises((HTTPException, httpx.HTTPError)):
            await remote_validator(token)


# ============ Tier Extraction Tests ============


class TestTierExtraction:
    """用户层级提取测试"""

    def test_admin_role_gets_admin_tier(self):
        """测试 admin 角色获得 admin 层级"""
        roles = ["user", "admin"]
        tier = "normal"

        # 模拟中间件中的层级提取逻辑
        if "admin" in roles:
            tier = "admin"
        elif "enterprise" in roles:
            tier = "enterprise"
        elif "premium" in roles or "vip" in roles:
            tier = "premium"

        assert tier == "admin"

    def test_enterprise_role_gets_enterprise_tier(self):
        """测试 enterprise 角色获得 enterprise 层级"""
        roles = ["user", "enterprise"]
        tier = "normal"

        if "admin" in roles:
            tier = "admin"
        elif "enterprise" in roles:
            tier = "enterprise"
        elif "premium" in roles or "vip" in roles:
            tier = "premium"

        assert tier == "enterprise"

    def test_vip_role_gets_premium_tier(self):
        """测试 vip 角色获得 premium 层级"""
        roles = ["user", "vip"]
        tier = "normal"

        if "admin" in roles:
            tier = "admin"
        elif "enterprise" in roles:
            tier = "enterprise"
        elif "premium" in roles or "vip" in roles:
            tier = "premium"

        assert tier == "premium"

    def test_normal_user_gets_normal_tier(self):
        """测试普通用户获得 normal 层级"""
        roles = ["user"]
        tier = "normal"

        if "admin" in roles:
            tier = "admin"
        elif "enterprise" in roles:
            tier = "enterprise"
        elif "premium" in roles or "vip" in roles:
            tier = "premium"

        assert tier == "normal"


# ============ Edge Cases Tests ============


class TestAuthEdgeCases:
    """认证边界情况测试"""

    def test_empty_bearer_token(self):
        """测试空 Bearer Token"""
        auth_header = "Bearer "
        token = auth_header[7:].strip()

        assert token == ""

    def test_bearer_case_insensitive(self):
        """测试 Bearer 大小写不敏感"""
        auth_headers = [
            "Bearer token123",
            "bearer token123",
            "BEARER token123",
        ]

        for auth_header in auth_headers:
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
                assert token == "token123"

    def test_roles_as_string_converted_to_list(self):
        """测试字符串角色转换为列表"""
        roles = "admin"
        if isinstance(roles, str):
            roles = [roles]

        assert isinstance(roles, list)
        assert "admin" in roles

    def test_missing_user_id_in_payload(self):
        """测试 payload 中缺少 user_id"""
        payload = {"tenant_id": "tenant_001"}

        user_id = payload.get("sub") or payload.get("user_id")

        assert user_id is None

    def test_sub_claim_fallback(self):
        """测试 sub claim 回退"""
        payload = {"sub": "user_from_sub", "tenant_id": "tenant_001"}

        user_id = payload.get("sub") or payload.get("user_id")

        assert user_id == "user_from_sub"
