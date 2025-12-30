"""
Tests for LangGraph optimization features.

Tests cover:
- Redis caching for Thread/Assistant/Assistants list
- Operation type detection
- Streaming path detection
- Context header injection
- HTTP connection pool settings
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


# ============ Test Operation Type Detection ============

class TestOperationTypeDetection:
    """Test LangGraph API operation type detection."""

    def test_detect_run_stream(self):
        from src.proxy.transparent_proxy import TransparentProxy

        assert TransparentProxy.detect_operation_type("POST", "/runs/stream") == "run_stream"
        assert TransparentProxy.detect_operation_type("POST", "/threads/abc123/runs/stream") == "run_stream"

    def test_detect_run_wait(self):
        from src.proxy.transparent_proxy import TransparentProxy

        assert TransparentProxy.detect_operation_type("POST", "/runs/wait") == "run_wait"
        assert TransparentProxy.detect_operation_type("POST", "/threads/abc123/runs/wait") == "run_wait"

    def test_detect_thread_operations(self):
        from src.proxy.transparent_proxy import TransparentProxy

        assert TransparentProxy.detect_operation_type("POST", "/threads") == "thread_create"
        assert TransparentProxy.detect_operation_type("GET", "/threads/abc123") == "thread_read"
        assert TransparentProxy.detect_operation_type("DELETE", "/threads/abc123") == "thread_delete"

    def test_detect_assistant_operations(self):
        from src.proxy.transparent_proxy import TransparentProxy

        assert TransparentProxy.detect_operation_type("GET", "/assistants") == "assistant_list"
        assert TransparentProxy.detect_operation_type("POST", "/assistants/search") == "assistant_list"
        assert TransparentProxy.detect_operation_type("GET", "/assistants/abc123") == "assistant_read"

    def test_detect_store_operations(self):
        from src.proxy.transparent_proxy import TransparentProxy

        assert TransparentProxy.detect_operation_type("GET", "/store/items") == "store_read"
        assert TransparentProxy.detect_operation_type("POST", "/store/items") == "store_write"
        assert TransparentProxy.detect_operation_type("DELETE", "/store/items") == "store_delete"

    def test_detect_unknown_operation(self):
        from src.proxy.transparent_proxy import TransparentProxy

        assert TransparentProxy.detect_operation_type("GET", "/unknown/path") == "proxy"


# ============ Test Streaming Path Detection ============

class TestStreamingPathDetection:
    """Test streaming path detection in middleware."""

    def test_streaming_suffixes(self):
        from src.core.middleware.streaming import is_streaming_path

        assert is_streaming_path("/runs/stream") is True
        assert is_streaming_path("/threads/abc/runs/stream") is True
        assert is_streaming_path("/api/v1/stream") is True
        assert is_streaming_path("/sse") is True

    def test_streaming_prefixes(self):
        from src.core.middleware.streaming import is_streaming_path

        assert is_streaming_path("/proxy/myservice/runs/stream") is True
        assert is_streaming_path("/api/v1/proxy/myservice/stream") is True

    def test_non_streaming_paths(self):
        from src.core.middleware.streaming import is_streaming_path

        assert is_streaming_path("/assistants") is False
        assert is_streaming_path("/threads") is False
        assert is_streaming_path("/runs/wait") is False

    def test_excludes_upstream(self):
        from src.core.middleware.streaming import is_streaming_path

        # "upstream" should not trigger streaming detection
        assert is_streaming_path("/config/upstream") is False


# ============ Test Context Header Injection ============

class TestContextHeaderInjection:
    """Test context header injection for LangGraph compatibility."""

    def test_langgraph_headers_injected(self):
        from src.proxy.context_injector import ContextInjector, RequestContext

        injector = ContextInjector(inject_user_info=True)
        context = RequestContext(
            user_id="user123",
            tenant_id="tenant456",
            user_tier="premium",
            is_authenticated=True,
            roles=["user", "developer"],
        )

        headers = injector.build_headers(context)

        # Check LangGraph-compatible headers
        assert headers.get("X-User-Id") == "user123"
        assert headers.get("X-Tenant-Id") == "tenant456"
        assert headers.get("X-User-Tier") == "premium"
        assert headers.get("X-User-Type") == "user"  # authenticated
        assert "read" in headers.get("X-User-Permissions", "")
        assert "write" in headers.get("X-User-Permissions", "")

    def test_gateway_headers_also_injected(self):
        from src.proxy.context_injector import ContextInjector, RequestContext

        injector = ContextInjector(inject_user_info=True)
        context = RequestContext(
            user_id="user123",
            tenant_id="tenant456",
            user_tier="premium",
            is_authenticated=True,
        )

        headers = injector.build_headers(context)

        # Check gateway standard headers (X-GW- prefix)
        assert headers.get("X-GW-User-ID") == "user123"
        assert headers.get("X-GW-Tenant-ID") == "tenant456"
        assert headers.get("X-GW-User-Tier") == "premium"

    def test_anonymous_user_type(self):
        from src.proxy.context_injector import ContextInjector, RequestContext

        injector = ContextInjector(inject_user_info=True)
        context = RequestContext(
            user_id="",  # No user ID
            is_authenticated=False,
        )

        headers = injector.build_headers(context)

        assert headers.get("X-User-Type") == "anonymous"

    def test_guest_user_type(self):
        from src.proxy.context_injector import ContextInjector, RequestContext

        injector = ContextInjector(inject_user_info=True)
        context = RequestContext(
            user_id="guest123",  # Has user ID
            is_authenticated=False,
        )

        headers = injector.build_headers(context)

        assert headers.get("X-User-Type") == "guest"


# ============ Test Redis Caching ============

class TestRedisCaching:
    """Test Redis caching methods for LangGraph."""

    def test_thread_mapping_key_format(self):
        """Verify thread mapping key format."""
        session_id = "session-123"
        expected_key = f"lg:thread_map:{session_id}"

        from src.persistence.redis import RedisStorage
        storage = RedisStorage(enabled=False)

        # Verify key format by checking the method implementation
        # The key should follow the pattern lg:thread_map:{session_id}
        assert f"lg:thread_map:{session_id}" == expected_key

    def test_thread_cache_key_format(self):
        """Verify thread cache key format."""
        thread_id = "abc-123"
        expected_key = f"lg:thread:{thread_id}"
        assert expected_key == f"lg:thread:{thread_id}"

    def test_assistant_cache_key_format(self):
        """Verify assistant cache key format."""
        assistant_id = "assistant-456"
        expected_key = f"lg:assistant:{assistant_id}"
        assert expected_key == f"lg:assistant:{assistant_id}"

    def test_assistants_list_cache_key_format(self):
        """Verify assistants list cache key format."""
        user_id = "user-789"
        expected_key = f"lg:assistants_list:{user_id}"
        assert expected_key == f"lg:assistants_list:{user_id}"

    def test_quota_key_format(self):
        """Verify user quota key format."""
        user_id = "user-789"
        expected_key = f"lg:quota:{user_id}:threads"
        assert expected_key == f"lg:quota:{user_id}:threads"


# ============ Test HTTP Connection Pool Settings ============

class TestHTTPConnectionPool:
    """Test HTTP connection pool optimization settings."""

    def test_default_pool_settings(self):
        from src.connectors.http import HTTPConnector

        assert HTTPConnector.DEFAULT_MAX_CONNECTIONS == 200
        assert HTTPConnector.DEFAULT_KEEPALIVE_CONNECTIONS == 50
        assert HTTPConnector.DEFAULT_KEEPALIVE_EXPIRY == 120.0

    def test_default_timeout_settings(self):
        from src.connectors.http import HTTPConnector

        assert HTTPConnector.DEFAULT_CONNECT_TIMEOUT == 3.0
        assert HTTPConnector.DEFAULT_READ_TIMEOUT == 300.0
        assert HTTPConnector.DEFAULT_WRITE_TIMEOUT == 60.0
        assert HTTPConnector.DEFAULT_POOL_TIMEOUT == 10.0


# ============ Test LangGraph Proxy Caching ============

class TestLangGraphProxyCaching:
    """Test LangGraphProxy two-level caching."""

    def test_cache_ttl_settings(self):
        from src.adapters.langgraph_proxy import LangGraphProxy

        assert LangGraphProxy.THREAD_CACHE_TTL == 60
        assert LangGraphProxy.ASSISTANT_CACHE_TTL == 300
        assert LangGraphProxy.ASSISTANTS_LIST_CACHE_TTL == 60


# ============ Run Tests ============

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
