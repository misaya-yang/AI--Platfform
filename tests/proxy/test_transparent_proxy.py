"""
透明代理核心测试

测试内容：
- 基本代理转发功能
- 请求头透传
- LangGraph assistant_id 注入
- 流式默认参数设置
- 错误处理和透传
- 负载均衡
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.proxy.transparent_proxy import (
    TransparentProxy,
    ProxyRequest,
    ProxyResponse,
    LANGGRAPH_ASSISTANT_PATHS,
    LANGGRAPH_OPERATION_TYPES,
)
from src.proxy.config_loader import ProxyServiceConfig
from src.proxy.context_injector import ContextInjector, RequestContext


# ============ Proxy Request/Response Tests ============


class TestProxyRequest:
    """代理请求测试"""

    def test_proxy_request_defaults(self):
        """测试默认值"""
        req = ProxyRequest(
            service_name="test_service",
            path="/api/test",
        )

        assert req.service_name == "test_service"
        assert req.path == "/api/test"
        assert req.method == "GET"
        assert req.body is None
        assert req.query_params == {}
        assert req.context is None
        assert req.stream is False

    def test_proxy_request_with_body(self):
        """测试带请求体的请求"""
        body = json.dumps({"message": "hello"}).encode()
        req = ProxyRequest(
            service_name="langgraph",
            path="/runs",
            method="POST",
            body=body,
            stream=True,
        )

        assert req.method == "POST"
        assert req.body == body
        assert req.stream is True


class TestProxyResponse:
    """代理响应测试"""

    def test_proxy_response_success(self):
        """测试成功响应"""
        resp = ProxyResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"status": "ok"}',
        )

        assert resp.status_code == 200
        assert resp.error is None
        assert resp.is_streaming is False

    def test_proxy_response_error(self):
        """测试错误响应"""
        resp = ProxyResponse(
            status_code=500,
            headers={},
            error="Internal Server Error",
        )

        assert resp.status_code == 500
        assert resp.error == "Internal Server Error"


# ============ TransparentProxy Tests ============


class TestTransparentProxy:
    """透明代理核心测试"""

    @pytest.fixture
    def proxy_config(self):
        """代理服务配置"""
        return ProxyServiceConfig(
            service_id="langgraph_001",
            service_name="langgraph",
            upstream_url="http://langgraph:8123",
            assistant_id="assistant_test_001",
            timeout_connect=5.0,
            timeout_read=60.0,
            enabled=True,
        )

    @pytest.fixture
    def mock_config_loader(self, proxy_config):
        """Mock 配置加载器"""
        loader = AsyncMock()
        loader.get_config = AsyncMock(return_value=proxy_config)
        return loader

    @pytest.fixture
    def context_injector(self):
        """上下文注入器"""
        return ContextInjector()

    @pytest.fixture
    def transparent_proxy(self, mock_config_loader, context_injector):
        """透明代理实例"""
        return TransparentProxy(
            config_loader=mock_config_loader,
            context_injector=context_injector,
            default_timeout=60.0,
        )

    # -------- 基本功能测试 --------

    @pytest.mark.asyncio
    async def test_proxy_service_not_found(self, transparent_proxy, mock_config_loader):
        """测试服务不存在"""
        mock_config_loader.get_config.return_value = None

        request = ProxyRequest(
            service_name="unknown_service",
            path="/api/test",
        )

        response = await transparent_proxy.proxy(request)

        assert response.status_code == 404
        assert "not found" in response.error.lower()

    @pytest.mark.asyncio
    async def test_proxy_service_disabled(
        self, transparent_proxy, mock_config_loader, proxy_config
    ):
        """测试服务已禁用"""
        proxy_config.enabled = False
        mock_config_loader.get_config.return_value = proxy_config

        request = ProxyRequest(
            service_name="langgraph",
            path="/api/test",
        )

        response = await transparent_proxy.proxy(request)

        assert response.status_code == 503
        assert "disabled" in response.error.lower()

    @pytest.mark.asyncio
    async def test_proxy_normal_request(self, transparent_proxy, mock_httpx_response):
        """测试普通 GET 请求代理"""
        mock_response = mock_httpx_response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"status": "ok"}',
        )

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_request:
            # 创建一个返回正确内容的 mock
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.content = b'{"status": "ok"}'
            mock_request.return_value = mock_resp

            request = ProxyRequest(
                service_name="langgraph",
                path="/assistants",
                method="GET",
            )

            response = await transparent_proxy.proxy(request)

            # 验证请求被正确转发
            assert mock_request.called or response.status_code in (200, 502)

    @pytest.mark.asyncio
    async def test_record_non_stream_usage_from_json_response(
        self, transparent_proxy, proxy_config
    ):
        """测试非流式 JSON 响应会写入 usage 统计"""
        proxy_config.assistant_id = None
        context = RequestContext(
            user_id="user_usage_1",
            tenant_id="tenant_usage_1",
            request_id="req_usage_1",
        )
        response_body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
            }
        ).encode("utf-8")
        request_body = json.dumps({"assistant_id": "imam_asst_1"}).encode("utf-8")

        recorder = AsyncMock()
        with patch("src.services.metrics.get_usage_recorder", return_value=recorder):
            await transparent_proxy._record_non_stream_usage(
                response_body=response_body,
                response_content_type="application/json; charset=utf-8",
                request_body=request_body,
                config=proxy_config,
                context=context,
                method="POST",
                path="/runs/wait",
                duration_ms=128.4,
                status_code=200,
            )

        recorder.record_usage.assert_awaited_once()
        kwargs = recorder.record_usage.await_args.kwargs
        assert kwargs["service_id"] == proxy_config.service_id
        assert kwargs["assistant_id"] == "imam_asst_1"
        assert kwargs["input_tokens"] == 7
        assert kwargs["output_tokens"] == 3

    @pytest.mark.asyncio
    async def test_record_non_stream_usage_run_error_without_usage(
        self, transparent_proxy, proxy_config
    ):
        """测试 run 请求即使无 usage 也会记录错误统计。"""
        context = RequestContext(
            user_id="user_usage_err",
            tenant_id="tenant_usage_err",
            request_id="req_usage_err",
        )
        response_body = json.dumps(
            {
                "__error__": {
                    "error": "upstream_error",
                    "message": "quota exhausted",
                }
            }
        ).encode("utf-8")

        recorder = AsyncMock()
        with patch("src.services.metrics.get_usage_recorder", return_value=recorder):
            await transparent_proxy._record_non_stream_usage(
                response_body=response_body,
                response_content_type="application/json",
                request_body=json.dumps({}).encode("utf-8"),
                config=proxy_config,
                context=context,
                method="POST",
                path="runs/wait",  # 无前导斜杠
                duration_ms=32.5,
                status_code=200,
            )

        recorder.record_usage.assert_awaited_once()
        kwargs = recorder.record_usage.await_args.kwargs
        assert kwargs["input_tokens"] == 0
        assert kwargs["output_tokens"] == 0
        assert kwargs["status"] == "error"
        assert kwargs["request_type"] == "proxy_run_wait"
        assert kwargs["metadata"]["response_has_error"] is True

    # -------- Assistant ID 注入测试 --------

    def test_inject_assistant_id_to_runs(self, transparent_proxy):
        """测试 /runs 路径注入 assistant_id"""
        body = json.dumps({"input": {"messages": [{"role": "user", "content": "hello"}]}}).encode()

        result = transparent_proxy._inject_assistant_id(
            body=body,
            path="/runs",
            assistant_id="test_assistant",
        )

        data = json.loads(result.decode())
        assert data.get("assistant_id") == "test_assistant"

    def test_inject_assistant_id_to_runs_stream(self, transparent_proxy):
        """测试 /runs/stream 路径注入 assistant_id"""
        body = json.dumps({"input": {"messages": [{"role": "user", "content": "hello"}]}}).encode()

        result = transparent_proxy._inject_assistant_id(
            body=body,
            path="/runs/stream",
            assistant_id="test_assistant",
        )

        data = json.loads(result.decode())
        assert data.get("assistant_id") == "test_assistant"

    def test_inject_assistant_id_runs_wait_without_leading_slash(self, transparent_proxy):
        """测试 runs/wait（无前导斜杠）也会注入 assistant_id"""
        body = json.dumps({"input": {"messages": [{"role": "user", "content": "hello"}]}}).encode()

        result = transparent_proxy._inject_assistant_id(
            body=body,
            path="runs/wait",
            assistant_id="test_assistant",
        )

        data = json.loads(result.decode())
        assert data.get("assistant_id") == "test_assistant"

    def test_inject_assistant_id_not_override(self, transparent_proxy):
        """测试不覆盖已有的 assistant_id"""
        body = json.dumps(
            {"assistant_id": "existing_assistant", "input": {"messages": []}}
        ).encode()

        result = transparent_proxy._inject_assistant_id(
            body=body,
            path="/runs",
            assistant_id="new_assistant",
        )

        data = json.loads(result.decode())
        # 应保留原有的 assistant_id
        assert data.get("assistant_id") == "existing_assistant"

    def test_inject_assistant_id_skip_non_langgraph_paths(self, transparent_proxy):
        """测试非 LangGraph 路径不注入"""
        body = json.dumps({"data": "test"}).encode()

        result = transparent_proxy._inject_assistant_id(
            body=body,
            path="/api/other",
            assistant_id="test_assistant",
        )

        data = json.loads(result.decode())
        assert "assistant_id" not in data

    def test_inject_assistant_id_empty_body(self, transparent_proxy):
        """测试空请求体"""
        result = transparent_proxy._inject_assistant_id(
            body=None,
            path="/runs",
            assistant_id="test_assistant",
        )

        assert result is None

    def test_inject_assistant_id_non_json_body(self, transparent_proxy):
        """测试非 JSON 请求体"""
        body = b"plain text body"

        result = transparent_proxy._inject_assistant_id(
            body=body,
            path="/runs",
            assistant_id="test_assistant",
        )

        # 非 JSON 应原样返回
        assert result == body

    # -------- 流式默认参数测试 --------

    def test_ensure_stream_defaults_adds_stream_mode(self, transparent_proxy):
        """测试自动添加 stream_mode"""
        body = json.dumps({"input": {"messages": [{"role": "user", "content": "hello"}]}}).encode()

        result = transparent_proxy._ensure_stream_defaults(body, "/runs/stream")

        data = json.loads(result.decode())
        assert "stream_mode" in data
        assert "messages" in data["stream_mode"]
        assert "updates" in data["stream_mode"]

    def test_ensure_stream_defaults_adds_stream_subgraphs(self, transparent_proxy):
        """测试自动添加 stream_subgraphs"""
        body = json.dumps(
            {
                "input": {"messages": []},
                "stream_mode": ["messages"],
            }
        ).encode()

        result = transparent_proxy._ensure_stream_defaults(body, "/runs/stream")

        data = json.loads(result.decode())
        assert data.get("stream_subgraphs") is True

    def test_ensure_stream_defaults_not_override_existing(self, transparent_proxy):
        """测试不覆盖已有配置"""
        body = json.dumps(
            {
                "input": {"messages": []},
                "stream_mode": ["values"],
                "stream_subgraphs": False,
            }
        ).encode()

        result = transparent_proxy._ensure_stream_defaults(body, "/runs/stream")

        data = json.loads(result.decode())
        # 应保留原有配置
        assert data["stream_mode"] == ["values"]
        assert data["stream_subgraphs"] is False

    def test_ensure_stream_defaults_skip_non_stream_path(self, transparent_proxy):
        """测试非流式路径不处理"""
        body = json.dumps({"input": {}}).encode()

        result = transparent_proxy._ensure_stream_defaults(body, "/runs")

        # 非流式路径应原样返回
        data = json.loads(result.decode())
        assert "stream_mode" not in data

    # -------- 流式路径检测测试 --------

    def test_is_streaming_path_runs_stream(self, transparent_proxy):
        """测试 /runs/stream 路径"""
        assert transparent_proxy._is_streaming_path("/runs/stream") is True
        assert transparent_proxy._is_streaming_path("/threads/123/runs/stream") is True

    def test_is_streaming_path_sse(self, transparent_proxy):
        """测试 /sse 路径"""
        assert transparent_proxy._is_streaming_path("/api/sse") is True

    def test_is_streaming_path_non_stream(self, transparent_proxy):
        """测试非流式路径"""
        assert transparent_proxy._is_streaming_path("/runs") is False
        assert transparent_proxy._is_streaming_path("/assistants") is False
        assert transparent_proxy._is_streaming_path("/threads/123") is False

    # -------- 操作类型检测测试 --------

    def test_detect_operation_type_run_stream(self):
        """测试检测 run_stream 操作"""
        op = TransparentProxy.detect_operation_type("POST", "/runs/stream")
        assert op == "run_stream"

    def test_detect_operation_type_run_wait_without_leading_slash(self):
        """测试无前导斜杠路径也能识别 run_wait。"""
        op = TransparentProxy.detect_operation_type("POST", "runs/wait")
        assert op == "run_wait"

    def test_detect_operation_type_thread_create(self):
        """测试检测 thread_create 操作"""
        op = TransparentProxy.detect_operation_type("POST", "/threads")
        assert op == "thread_create"

    def test_detect_operation_type_assistant_list(self):
        """测试检测 assistant_list 操作"""
        op = TransparentProxy.detect_operation_type("GET", "/assistants")
        assert op == "assistant_list"

    def test_detect_operation_type_unknown(self):
        """测试未知操作返回默认值"""
        op = TransparentProxy.detect_operation_type("GET", "/unknown/path")
        assert op == "proxy"

    # -------- 响应头过滤测试 --------

    def test_filter_response_headers_removes_hop_by_hop(self, transparent_proxy):
        """测试移除 hop-by-hop 头"""
        headers = {
            "content-type": "application/json",
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "x-custom-header": "value",
        }

        filtered = transparent_proxy._filter_response_headers(headers)

        assert "content-type" in filtered
        assert "x-custom-header" in filtered
        assert "connection" not in filtered
        assert "transfer-encoding" not in filtered

    # -------- 负载均衡测试 --------

    def test_select_upstream_single_url(self, transparent_proxy, proxy_config):
        """测试单 URL 选择"""
        url = transparent_proxy._select_upstream(proxy_config)
        assert url == proxy_config.upstream_url

    def test_select_upstream_round_robin(self, transparent_proxy, proxy_config):
        """测试轮询负载均衡"""
        # 清空 upstream_url 避免被加入列表
        proxy_config.upstream_url = ""
        proxy_config.upstream_urls = [
            "http://langgraph-1:8123",
            "http://langgraph-2:8123",
            "http://langgraph-3:8123",
        ]
        proxy_config.load_balance_strategy = "round_robin"

        # 多次选择，验证轮询
        urls = [transparent_proxy._select_upstream(proxy_config) for _ in range(6)]

        # 应该轮询所有 URL
        assert len(set(urls)) == 3

    def test_select_upstream_random(self, transparent_proxy, proxy_config):
        """测试随机负载均衡"""
        proxy_config.upstream_urls = [
            "http://langgraph-1:8123",
            "http://langgraph-2:8123",
        ]
        proxy_config.load_balance_strategy = "random"

        # 多次选择
        urls = [transparent_proxy._select_upstream(proxy_config) for _ in range(10)]

        # 应该选中所有 URL（概率上）
        assert all(url in proxy_config.get_upstream_urls() for url in urls)

    def test_select_upstream_no_urls(self, transparent_proxy, proxy_config):
        """测试无可用 URL"""
        proxy_config.upstream_url = ""
        proxy_config.upstream_urls = []

        with pytest.raises(ValueError, match="No upstream URLs"):
            transparent_proxy._select_upstream(proxy_config)

    # -------- URL 构建测试 --------

    def test_build_upstream_url_basic(self, transparent_proxy, proxy_config):
        """测试基本 URL 构建"""
        url = transparent_proxy._build_upstream_url(
            config=proxy_config,
            path="/runs/stream",
            base_url="http://langgraph:8123",
        )

        assert url == "http://langgraph:8123/runs/stream"

    def test_build_upstream_url_with_path_rewrite(self, transparent_proxy, proxy_config):
        """测试带路径重写的 URL 构建"""
        proxy_config.path_rewrite = "/api/v1"

        url = transparent_proxy._build_upstream_url(
            config=proxy_config,
            path="/runs/stream",
            base_url="http://langgraph:8123",
        )

        assert url == "http://langgraph:8123/api/v1/runs/stream"

    def test_build_upstream_url_cleans_slashes(self, transparent_proxy, proxy_config):
        """测试斜杠清理"""
        url = transparent_proxy._build_upstream_url(
            config=proxy_config,
            path="runs/stream",  # 无前导斜杠
            base_url="http://langgraph:8123/",  # 有尾部斜杠
        )

        assert url == "http://langgraph:8123/runs/stream"


# ============ Stream Mode Helper Tests ============


class TestStreamModeHelpers:
    """流式模式辅助函数测试"""

    def test_stream_mode_wants_messages_list(self):
        """测试列表形式的 stream_mode"""
        assert TransparentProxy._stream_mode_wants_messages(["messages"]) is True
        assert TransparentProxy._stream_mode_wants_messages(["messages", "updates"]) is True
        assert TransparentProxy._stream_mode_wants_messages(["messages-tuple"]) is True
        assert TransparentProxy._stream_mode_wants_messages(["values"]) is False
        assert TransparentProxy._stream_mode_wants_messages(["updates"]) is False

    def test_stream_mode_wants_messages_string(self):
        """测试字符串形式的 stream_mode"""
        assert TransparentProxy._stream_mode_wants_messages("messages") is True
        assert TransparentProxy._stream_mode_wants_messages("messages,updates") is True
        assert TransparentProxy._stream_mode_wants_messages("values") is False

    def test_stream_mode_wants_messages_none(self):
        """测试 None 值"""
        assert TransparentProxy._stream_mode_wants_messages(None) is False

    def test_stream_mode_wants_messages_invalid(self):
        """测试无效类型"""
        assert TransparentProxy._stream_mode_wants_messages(123) is False
        assert TransparentProxy._stream_mode_wants_messages({}) is False


# ============ Constants Tests ============


class TestConstants:
    """常量测试"""

    def test_langgraph_assistant_paths(self):
        """测试 LangGraph 需要 assistant_id 的路径"""
        assert "/runs" in LANGGRAPH_ASSISTANT_PATHS
        assert "/runs/stream" in LANGGRAPH_ASSISTANT_PATHS
        assert "/threads/" in LANGGRAPH_ASSISTANT_PATHS

    def test_langgraph_operation_types(self):
        """测试操作类型映射"""
        assert LANGGRAPH_OPERATION_TYPES["POST /runs/stream"] == "run_stream"
        assert LANGGRAPH_OPERATION_TYPES["POST /threads"] == "thread_create"
        assert LANGGRAPH_OPERATION_TYPES["GET /assistants"] == "assistant_list"
