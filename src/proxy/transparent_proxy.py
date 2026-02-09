"""
透明代理核心

使用 httpx.AsyncClient 将原始请求透明转发至目标服务，支持：
- 动态路由转发
- 完美的 SSE 流式传输
- 请求/响应头处理
- 负载均衡
- LangGraph assistant_id 自动注入
- 上游错误透传（4xx/5xx）
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from .billing_interceptor import BillingInterceptor, StreamProcessor
from .config_loader import ProxyConfigLoader, ProxyServiceConfig
from .context_injector import ContextInjector, RequestContext
from ..core.observability.logging import get_logger
from ..services.metrics.usage_parser import (
    extract_assistant_id,
    extract_model,
    extract_provider,
    extract_token_usage,
)

logger = get_logger(__name__)


# LangGraph 需要 assistant_id 的路径模式
LANGGRAPH_ASSISTANT_PATHS = [
    "/runs",  # POST /runs
    "/runs/stream",  # POST /runs/stream
    "/runs/wait",  # POST /runs/wait
    "/threads/",  # POST /threads/{thread_id}/runs etc.
]

# LangGraph API 操作类型映射（用于限流）
LANGGRAPH_OPERATION_TYPES = {
    # Assistants API
    "GET /assistants": "assistant_list",
    "POST /assistants": "assistant_create",
    "POST /assistants/search": "assistant_list",
    "GET /assistants/": "assistant_read",
    "PATCH /assistants/": "assistant_update",
    "DELETE /assistants/": "assistant_delete",
    # Threads API
    "POST /threads": "thread_create",
    "POST /threads/search": "thread_search",
    "GET /threads/": "thread_read",
    "PATCH /threads/": "thread_update",
    "DELETE /threads/": "thread_delete",
    # Thread State API
    "GET /threads/*/state": "state_read",
    "POST /threads/*/state": "state_update",
    "GET /threads/*/history": "history_read",
    # Runs API
    "POST /threads/*/runs": "run_create",
    "POST /threads/*/runs/stream": "run_stream",
    "POST /threads/*/runs/wait": "run_wait",
    "GET /threads/*/runs": "run_list",
    "GET /threads/*/runs/": "run_read",
    "POST /threads/*/runs/*/cancel": "run_cancel",
    "POST /threads/*/runs/*/join": "run_join",
    # Stateless Runs API
    "POST /runs": "run_create",
    "POST /runs/stream": "run_stream",
    "POST /runs/wait": "run_wait",
    "GET /runs/": "run_read",
    # Store API
    "GET /store/items": "store_read",
    "POST /store/items": "store_write",
    "DELETE /store/items": "store_delete",
    "GET /store/namespaces": "store_list",
    "POST /store/namespaces": "store_list",
    # Crons API
    "GET /crons": "cron_list",
    "POST /crons": "cron_create",
    "GET /crons/": "cron_read",
    "PATCH /crons/": "cron_update",
    "DELETE /crons/": "cron_delete",
    "POST /crons/*/trigger": "cron_trigger",
}


@dataclass
class ProxyRequest:
    """代理请求"""

    # 目标服务
    service_name: str

    # 请求路径（不含 /proxy/{service_name} 前缀）
    path: str

    # HTTP 方法
    method: str = "GET"

    # 请求体
    body: Optional[bytes] = None

    # 查询参数
    query_params: Dict[str, Any] = field(default_factory=dict)

    # 请求上下文
    context: Optional[RequestContext] = None

    # 是否期望流式响应
    stream: bool = False


@dataclass
class ProxyResponse:
    """代理响应"""

    status_code: int
    headers: Dict[str, str]
    body: Optional[bytes] = None

    # 流式响应迭代器
    stream: Optional[AsyncIterator[bytes]] = None

    # 是否是流式响应
    is_streaming: bool = False

    # 错误信息
    error: Optional[str] = None


class TransparentProxy:
    """
    透明代理核心

    负责：
    - 从配置加载器获取目标服务信息
    - 构建上游请求
    - 转发请求并处理响应
    - 流式响应处理
    """

    def __init__(
        self,
        config_loader: ProxyConfigLoader,
        context_injector: Optional[ContextInjector] = None,
        billing_interceptor: Optional[BillingInterceptor] = None,
        default_timeout: float = 60.0,
    ):
        """
        初始化透明代理

        Args:
            config_loader: 服务配置加载器
            context_injector: 上下文注入器
            billing_interceptor: 计费拦截器
            default_timeout: 默认超时时间（秒）
        """
        self.config_loader = config_loader
        self.context_injector = context_injector or ContextInjector()
        self.billing_interceptor = billing_interceptor
        self.default_timeout = default_timeout

        # HTTP 客户端池（按服务维护）
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._client_lock = asyncio.Lock()

        # 负载均衡状态
        self._lb_counters: Dict[str, int] = {}  # round-robin 计数器
        self._lb_connections: Dict[str, Dict[str, int]] = {}  # 连接计数

    async def close(self) -> None:
        """关闭所有 HTTP 客户端"""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        logger.info("Transparent proxy closed")

    async def _get_client(self, config: ProxyServiceConfig) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        client_key = config.service_id

        if client_key not in self._clients:
            async with self._client_lock:
                if client_key not in self._clients:
                    timeout = httpx.Timeout(
                        connect=config.timeout_connect,
                        read=config.timeout_read,
                        write=config.timeout_write,
                        pool=config.timeout_pool,
                    )
                    self._clients[client_key] = httpx.AsyncClient(
                        timeout=timeout,
                        follow_redirects=True,
                        http2=True,  # 启用 HTTP/2
                    )

        return self._clients[client_key]

    def _select_upstream(self, config: ProxyServiceConfig) -> str:
        """
        选择上游服务器（负载均衡）

        Args:
            config: 服务配置

        Returns:
            选中的上游 URL
        """
        urls = config.get_upstream_urls()
        if not urls:
            raise ValueError(f"No upstream URLs configured for {config.service_name}")

        if len(urls) == 1:
            return urls[0]

        strategy = config.load_balance_strategy
        service_id = config.service_id

        if strategy == "round_robin":
            counter = self._lb_counters.get(service_id, -1)
            counter = (counter + 1) % len(urls)
            self._lb_counters[service_id] = counter
            return urls[counter]

        elif strategy == "least_connections":
            connections = self._lb_connections.get(service_id, {})
            min_conn = float("inf")
            selected = urls[0]
            for url in urls:
                conn = connections.get(url, 0)
                if conn < min_conn:
                    min_conn = conn
                    selected = url
            return selected

        elif strategy == "random":
            return random.choice(urls)

        else:
            # 默认 round_robin
            return urls[0]

    def _build_upstream_url(
        self,
        config: ProxyServiceConfig,
        path: str,
        base_url: str,
    ) -> str:
        """
        构建上游请求 URL

        Args:
            config: 服务配置
            path: 请求路径
            base_url: 上游基础 URL

        Returns:
            完整的上游 URL
        """
        # 清理 base_url 尾部斜杠
        base_url = base_url.rstrip("/")

        # 路径重写
        if config.path_rewrite:
            path = config.path_rewrite.rstrip("/") + "/" + path.lstrip("/")

        # 确保路径以 / 开头
        if not path.startswith("/"):
            path = "/" + path

        return base_url + path

    async def proxy(self, request: ProxyRequest) -> ProxyResponse:
        """
        执行代理请求

        Args:
            request: 代理请求

        Returns:
            代理响应
        """
        start_time = time.perf_counter()
        t_config_start = start_time

        # 1. 获取服务配置
        config = await self.config_loader.get_config(request.service_name)
        t_config_done = time.perf_counter()
        if not config:
            return ProxyResponse(
                status_code=404,
                headers={},
                error=f"Service not found: {request.service_name}",
            )

        if not config.enabled:
            return ProxyResponse(
                status_code=503,
                headers={},
                error=f"Service disabled: {request.service_name}",
            )

        # 2. 选择上游服务器
        try:
            upstream_base = self._select_upstream(config)
        except ValueError as e:
            return ProxyResponse(
                status_code=502,
                headers={},
                error=str(e),
            )

        # 3. 构建上游 URL
        upstream_url = self._build_upstream_url(config, request.path, upstream_base)

        # 4. 构建请求头
        context = request.context or RequestContext()
        headers = self.context_injector.build_headers(
            context=context,
            service_auth_token=config.auth_token if not config.forward_auth else None,
        )
        t_headers_done = time.perf_counter()

        # 5. 获取 HTTP 客户端
        client = await self._get_client(config)
        t_client_done = time.perf_counter()

        # 6. LangGraph assistant_id 自动注入 + 流式默认参数
        body = request.body
        if request.method in ("POST", "PUT", "PATCH"):
            if config.assistant_id:
                body = self._inject_assistant_id(body, request.path, config.assistant_id)
            # 为流式请求设置默认参数
            body = self._ensure_stream_defaults(body, request.path)
        t_body_done = time.perf_counter()

        # Performance logging
        logger.info(
            f"[Proxy][TIMING] {request.method} {request.service_name}/{request.path} prep: "
            f"config={((t_config_done - t_config_start) * 1000):.1f}ms "
            f"headers={((t_headers_done - t_config_done) * 1000):.1f}ms "
            f"client={((t_client_done - t_headers_done) * 1000):.1f}ms "
            f"body={((t_body_done - t_client_done) * 1000):.1f}ms "
            f"total_prep={((t_body_done - start_time) * 1000):.1f}ms"
        )

        # 7. 执行请求
        logger.info(
            f"[Proxy] {request.method} {request.service_name}/{request.path} -> {upstream_url}"
        )

        try:
            if request.stream or self._is_streaming_path(request.path):
                return await self._proxy_streaming(
                    client=client,
                    method=request.method,
                    url=upstream_url,
                    headers=headers,
                    body=body,
                    params=request.query_params,
                    config=config,
                    context=context,
                    path=request.path,
                )
            else:
                return await self._proxy_normal(
                    client=client,
                    method=request.method,
                    url=upstream_url,
                    upstream_base=upstream_base,
                    headers=headers,
                    body=body,
                    params=request.query_params,
                    path=request.path,
                    config=config,
                    context=context,
                )
        except httpx.TimeoutException as e:
            duration = (time.time() - start_time) * 1000
            logger.error(f"[Proxy] Timeout after {duration:.2f}ms: {e}")
            return ProxyResponse(
                status_code=504,
                headers={},
                error=f"Upstream timeout: {e}",
            )
        except httpx.RequestError as e:
            duration = (time.time() - start_time) * 1000
            logger.error(f"[Proxy] Request error after {duration:.2f}ms: {e}")
            return ProxyResponse(
                status_code=502,
                headers={},
                error=f"Upstream error: {e}",
            )

    def _inject_assistant_id(
        self,
        body: Optional[bytes],
        path: str,
        assistant_id: str,
    ) -> Optional[bytes]:
        """
        为 LangGraph 请求注入 assistant_id

        LangGraph API 的以下端点需要 assistant_id:
        - POST /runs
        - POST /runs/stream
        - POST /runs/wait
        - POST /threads/{thread_id}/runs
        - POST /threads/{thread_id}/runs/stream
        - POST /threads/{thread_id}/runs/wait

        如果请求体中已包含 assistant_id，则不覆盖。
        """
        if not body:
            return body

        # 检查是否是需要注入的路径
        path_lower = self._normalize_path(path).lower()
        needs_injection = any(pattern in path_lower for pattern in LANGGRAPH_ASSISTANT_PATHS)

        if not needs_injection:
            return body

        try:
            data = json.loads(body.decode("utf-8"))

            # 如果已有 assistant_id，不覆盖
            if isinstance(data, dict) and "assistant_id" not in data:
                data["assistant_id"] = assistant_id
                logger.debug(f"[Proxy] Injected assistant_id={assistant_id} into request body")
                return json.dumps(data).encode("utf-8")

            return body

        except (json.JSONDecodeError, UnicodeDecodeError):
            # 非 JSON 请求体，原样返回
            return body

    @staticmethod
    def _replace_assistant_id(body: Optional[bytes], assistant_id: str) -> Optional[bytes]:
        """强制替换请求体中的 assistant_id（用于重试自愈）。"""
        if not body:
            return body
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body
        if not isinstance(data, dict):
            return body
        data["assistant_id"] = assistant_id
        return json.dumps(data).encode("utf-8")

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = (path or "").strip()
        if not normalized:
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        return normalized

    @staticmethod
    def _stream_mode_wants_messages(stream_mode: Any) -> bool:
        """检查 stream_mode 是否包含 messages 模式"""
        if stream_mode is None:
            return False
        if isinstance(stream_mode, str):
            modes = [m.strip() for m in stream_mode.split(",") if m.strip()]
        elif isinstance(stream_mode, list):
            modes = [str(m).strip() for m in stream_mode if str(m).strip()]
        else:
            return False
        return any(m in ("messages", "messages-tuple") for m in modes)

    def _ensure_stream_defaults(self, body: Optional[bytes], path: str) -> Optional[bytes]:
        """
        为 LangGraph 流式请求设置默认参数

        确保 /runs/stream 请求包含合理的默认值：
        - stream_mode: ["messages", "updates"] - 启用消息流
        - stream_subgraphs: true - 启用子图流式输出
        """
        if not body:
            return body
        normalized_path = self._normalize_path(path)
        if not self._is_streaming_path(normalized_path):
            return body
        if "/runs/stream" not in normalized_path.lower():
            return body

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body

        if not isinstance(data, dict):
            return body

        changed = False

        # 设置默认 stream_mode
        if not data.get("stream_mode"):
            data["stream_mode"] = ["messages", "updates"]
            changed = True
            logger.debug("[Proxy] Set default stream_mode=['messages', 'updates']")

        # 如果 stream_mode 包含 messages，自动启用 stream_subgraphs
        if "stream_subgraphs" not in data and self._stream_mode_wants_messages(
            data.get("stream_mode")
        ):
            data["stream_subgraphs"] = True
            changed = True
            logger.debug("[Proxy] Set stream_subgraphs=true for messages mode")

        if not changed:
            return body

        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _parse_json_body(body: Optional[bytes]) -> Optional[Any]:
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _is_run_operation_path(path: str) -> bool:
        normalized = TransparentProxy._normalize_path(path).lower()
        return "/runs" in normalized

    @staticmethod
    def _extract_error_detail(body: bytes) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body.decode("utf-8", errors="ignore")
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if detail is not None:
                return str(detail)
        return str(payload)

    @staticmethod
    def _extract_assistant_records(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("items", "assistants", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _build_graph_candidates(self, config: ProxyServiceConfig) -> List[str]:
        candidates: List[str] = []

        def _add(value: Optional[str]) -> None:
            if not value:
                return
            normalized = str(value).strip()
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        _add(config.graph_id)
        _add(config.assistant_id)
        _add(config.service_name)
        _add(config.service_id)

        for token in re.split(r"[^A-Za-z0-9]+", config.service_id or ""):
            if not token or token.isdigit():
                continue
            _add(token)

        return candidates

    async def _resolve_assistant_retry_candidate(
        self,
        *,
        client: httpx.AsyncClient,
        config: ProxyServiceConfig,
        upstream_base: str,
        headers: Dict[str, str],
    ) -> Optional[str]:
        """
        基于上游 assistants/search 结果选择可用 assistant_id（只做无损自动修复）。
        """
        probe_url = self._build_upstream_url(config, "assistants/search", upstream_base)
        try:
            response = await client.post(
                probe_url,
                headers=headers,
                json={},
            )
        except Exception as exc:
            logger.debug(f"[Proxy] assistant retry probe failed: {exc}")
            return None

        if response.status_code >= 400:
            logger.debug(f"[Proxy] assistant retry probe status={response.status_code}")
            return None

        records = self._extract_assistant_records(self._parse_json_body(response.content))
        if not records:
            return None

        graph_map = {
            str(item.get("graph_id")).strip().lower(): str(item.get("graph_id")).strip()
            for item in records
            if item.get("graph_id")
        }
        assistant_map = {
            str(item.get("assistant_id")).strip().lower(): str(item.get("assistant_id")).strip()
            for item in records
            if item.get("assistant_id")
        }

        for candidate in self._build_graph_candidates(config):
            key = candidate.strip().lower()
            resolved = graph_map.get(key) or assistant_map.get(key)
            if resolved:
                return resolved

        return None

    async def _retry_on_invalid_assistant(
        self,
        *,
        response: httpx.Response,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        params: Dict[str, Any],
        path: str,
        config: ProxyServiceConfig,
        upstream_base: str,
    ) -> Tuple[httpx.Response, Optional[bytes]]:
        if response.status_code != 422 or not self._is_run_operation_path(path):
            return response, body

        detail = self._extract_error_detail(response.content)
        if "Invalid assistant" not in detail:
            return response, body

        retry_assistant = await self._resolve_assistant_retry_candidate(
            client=client,
            config=config,
            upstream_base=upstream_base,
            headers=headers,
        )
        if not retry_assistant:
            return response, body

        current_assistant = (
            str((self._parse_json_body(body) or {}).get("assistant_id") or config.assistant_id or "")
            .strip()
        )
        if retry_assistant == current_assistant:
            return response, body

        retry_body = self._replace_assistant_id(body, retry_assistant)
        if retry_body == body:
            return response, body

        logger.warning(
            "[Proxy] Auto-healing invalid assistant for service %s: %s -> %s",
            config.service_id,
            current_assistant or "<empty>",
            retry_assistant,
        )

        retry_response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=retry_body,
            params=params,
        )

        # Keep graph_id untouched; retry target can be either graph name or assistant UUID.
        config.assistant_id = retry_assistant

        return retry_response, retry_body

    @staticmethod
    def _extract_error_metadata(payload: Any) -> Dict[str, str]:
        if not isinstance(payload, dict):
            return {}
        error_payload = payload.get("__error__") or payload.get("error")
        if not error_payload:
            return {}

        if isinstance(error_payload, dict):
            error_type = str(
                error_payload.get("error")
                or error_payload.get("type")
                or "upstream_error"
            )
            error_message = str(
                error_payload.get("message")
                or error_payload.get("detail")
                or ""
            )
        else:
            error_type = "upstream_error"
            error_message = str(error_payload)

        metadata: Dict[str, str] = {"upstream_error_type": error_type[:128]}
        if error_message:
            metadata["upstream_error_message"] = error_message[:512]
        return metadata

    async def _record_non_stream_usage(
        self,
        response_body: bytes,
        response_content_type: str,
        request_body: Optional[bytes],
        config: ProxyServiceConfig,
        context: RequestContext,
        method: str,
        path: str,
        duration_ms: float,
        status_code: int,
    ) -> None:
        operation = self.detect_operation_type(method, path)
        is_run_operation = operation.startswith("run_")

        response_data = None
        if "application/json" in (response_content_type or "").lower():
            response_data = self._parse_json_body(response_body)

        # 非 run 请求仍保持「必须有 usage 才记录」策略，避免噪音数据。
        if response_data is None and not is_run_operation:
            return

        usage = extract_token_usage(response_data) if response_data is not None else None
        request_data = self._parse_json_body(request_body)

        input_tokens = int((usage or {}).get("input_tokens") or 0)
        output_tokens = int((usage or {}).get("output_tokens") or 0)
        total_tokens = int((usage or {}).get("total_tokens") or (input_tokens + output_tokens))
        has_usage = total_tokens > 0 or input_tokens > 0 or output_tokens > 0
        if not has_usage and not is_run_operation:
            return

        response_error = bool(response_data and isinstance(response_data, dict) and (
            "__error__" in response_data or "error" in response_data
        ))
        status = "error" if status_code >= 400 or response_error else "success"

        assistant_id = (
            config.assistant_id
            or extract_assistant_id(request_data)
            or extract_assistant_id(response_data)
            or ""
        )
        model = (
            extract_model(response_data)
            or extract_model(request_data)
            or ("langgraph-agent" if is_run_operation else "")
        )
        provider = extract_provider(response_data) or extract_provider(request_data) or ""

        request_type = f"proxy_{operation}"
        metadata: Dict[str, Any] = {
            "source": "transparent_proxy_non_stream",
            "path": self._normalize_path(path),
            "http_status": status_code,
            "response_has_usage": bool(usage),
            "response_has_error": response_error,
        }
        metadata.update(self._extract_error_metadata(response_data))

        try:
            from ..services.metrics import get_usage_recorder

            recorder = get_usage_recorder()
            await recorder.record_usage(
                tenant_id=context.tenant_id or "default",
                user_id=context.user_id or "anonymous",
                model=model or "unknown",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                request_id=context.request_id,
                service_id=config.service_id,
                assistant_id=assistant_id,
                provider=provider,
                latency_ms=int(duration_ms),
                status=status,
                request_type=request_type,
                metadata=metadata,
            )
        except Exception as e:
            logger.debug(f"[Proxy] Failed to record non-stream usage: {e}")

        try:
            if self.billing_interceptor and self.billing_interceptor.realtime_metrics:
                await self.billing_interceptor.realtime_metrics.record_token_usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
        except Exception as e:
            logger.debug(f"[Proxy] Failed to update realtime token metrics: {e}")

    async def _proxy_normal(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        upstream_base: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        params: Dict[str, Any],
        path: str,
        config: ProxyServiceConfig,
        context: RequestContext,
    ) -> ProxyResponse:
        """执行普通（非流式）代理请求"""
        request_started = time.perf_counter()
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
            params=params,
        )
        response, body = await self._retry_on_invalid_assistant(
            response=response,
            client=client,
            method=method,
            url=url,
            headers=headers,
            body=body,
            params=params,
            path=path,
            config=config,
            upstream_base=upstream_base,
        )

        duration_ms = (time.perf_counter() - request_started) * 1000

        # 提取响应头（过滤 hop-by-hop 头）
        response_headers = self._filter_response_headers(dict(response.headers))

        # 非流式路径也需要做 usage 统计（例如 /runs/wait）。
        await self._record_non_stream_usage(
            response_body=response.content,
            response_content_type=response_headers.get("content-type", ""),
            request_body=body,
            config=config,
            context=context,
            method=method,
            path=path,
            duration_ms=duration_ms,
            status_code=response.status_code,
        )

        # 错误透传：原样返回上游的 4xx/5xx 响应，不用网关错误覆盖
        # 这样前端可以获取到原始的业务错误信息
        return ProxyResponse(
            status_code=response.status_code,
            headers=response_headers,
            body=response.content,
            is_streaming=False,
            # 不设置 error，让上游的原始响应透传
        )

    async def _proxy_streaming(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        params: Dict[str, Any],
        config: ProxyServiceConfig,
        context: RequestContext,
        path: str,
    ) -> ProxyResponse:
        """
        执行流式代理请求

        支持：
        - 自动检测响应 Content-Type，如果不是 text/event-stream 则回退到普通响应
        - 错误透传：原样返回上游 4xx/5xx 错误
        - 计费拦截
        """
        request_started = time.perf_counter()
        request_data = self._parse_json_body(body)

        # 创建流处理器（用于计费）
        stream_processor: Optional[StreamProcessor] = None
        if self.billing_interceptor:
            assistant_id = config.assistant_id or extract_assistant_id(request_data) or ""
            operation = self.detect_operation_type(method, path)
            request_type = f"proxy_{operation}"
            stream_processor = self.billing_interceptor.create_stream_processor(
                request_id=context.request_id,
                service_id=config.service_id,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                assistant_id=assistant_id,
                request_type=request_type,
                model_hint=extract_model(request_data) or "",
                provider_hint=extract_provider(request_data) or "",
            )

        # 先获取响应头，判断是否真的是流式响应
        t_send_start = time.perf_counter()
        try:
            response = await client.send(
                client.build_request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body,
                    params=params,
                ),
                stream=True,
            )
            t_response_headers = time.perf_counter()
            logger.info(
                f"[Proxy][TIMING] streaming response_headers={((t_response_headers - t_send_start) * 1000):.1f}ms "
                f"status={response.status_code}"
            )
        except httpx.TimeoutException as e:
            logger.error(f"[Proxy] Streaming timeout: {e}")
            return ProxyResponse(
                status_code=504,
                headers={},
                error=f"Upstream timeout: {e}",
            )
        except httpx.RequestError as e:
            logger.error(f"[Proxy] Streaming request error: {e}")
            return ProxyResponse(
                status_code=502,
                headers={},
                error=f"Upstream error: {e}",
            )

        response_content_type = response.headers.get("content-type", "")
        response_headers = self._filter_response_headers(dict(response.headers))

        # 错误透传：4xx/5xx 错误原样返回，不覆盖
        if response.status_code >= 400:
            error_body = await response.aread()
            duration_ms = (time.perf_counter() - request_started) * 1000
            await self._record_non_stream_usage(
                response_body=error_body,
                response_content_type=response_content_type,
                request_body=body,
                config=config,
                context=context,
                method=method,
                path=path,
                duration_ms=duration_ms,
                status_code=response.status_code,
            )
            await response.aclose()
            logger.warning(
                f"[Proxy] Upstream error {response.status_code}: {error_body[:200].decode('utf-8', errors='ignore')}"
            )
            return ProxyResponse(
                status_code=response.status_code,
                headers=response_headers,
                body=error_body,
                is_streaming=False,
                # 不设置 error，保持原始响应内容
            )

        # 自动检测：如果响应不是流式的，直接返回普通响应
        is_sse = "text/event-stream" in response_content_type

        if not is_sse:
            # 非流式响应，读取完整内容
            body_content = await response.aread()
            await response.aclose()
            logger.debug(f"[Proxy] Non-streaming response detected: {response_content_type}")
            duration_ms = (time.perf_counter() - request_started) * 1000
            await self._record_non_stream_usage(
                response_body=body_content,
                response_content_type=response_content_type,
                request_body=body,
                config=config,
                context=context,
                method=method,
                path=path,
                duration_ms=duration_ms,
                status_code=response.status_code,
            )
            return ProxyResponse(
                status_code=response.status_code,
                headers=response_headers,
                body=body_content,
                is_streaming=False,
            )

        # 流式响应处理
        async def stream_generator():
            """流式响应生成器"""
            try:
                async for chunk in response.aiter_raw():
                    if stream_processor:
                        chunk = await stream_processor.process_chunk(chunk)
                    yield chunk

            except Exception as e:
                logger.error(f"[Proxy] Streaming error: {e}")
                # 发送错误事件
                error_event = f"event: error\ndata: {str(e)}\n\n"
                yield error_event.encode("utf-8")
            finally:
                if stream_processor:
                    try:
                        await stream_processor.finalize()
                    except Exception as finalize_err:
                        logger.debug(f"[Proxy] Failed to finalize stream processor: {finalize_err}")
                await response.aclose()

        # 返回流式响应，保留原始状态码
        return ProxyResponse(
            status_code=response.status_code,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
            },
            stream=stream_generator(),
            is_streaming=True,
        )

    def _is_streaming_path(self, path: str) -> bool:
        """
        检查是否是流式路径

        LangGraph 的流式端点：
        - /runs/stream
        - /threads/{thread_id}/runs/stream
        """
        streaming_suffixes = [
            "/stream",
            "/runs/stream",
            "/sse",
        ]

        path_lower = self._normalize_path(path).lower()

        # 精确匹配流式后缀
        for suffix in streaming_suffixes:
            if path_lower.endswith(suffix):
                return True

        # 检查路径中是否包含 "stream"（但要排除 "upstream" 等）
        if "/stream" in path_lower:
            return True

        return False

    @staticmethod
    def detect_operation_type(method: str, path: str) -> str:
        """
        检测 LangGraph API 操作类型

        用于更精细的限流控制。

        Args:
            method: HTTP 方法
            path: 请求路径

        Returns:
            操作类型字符串，如 "run_stream", "thread_create" 等
        """
        import re

        path_lower = TransparentProxy._normalize_path(path).lower()
        if path_lower != "/":
            path_lower = path_lower.rstrip("/")
        method_upper = method.upper()

        # 首先尝试精确匹配
        key = f"{method_upper} {path_lower}"
        if key in LANGGRAPH_OPERATION_TYPES:
            return LANGGRAPH_OPERATION_TYPES[key]

        # 尝试前缀匹配（带 /）
        for pattern, op_type in LANGGRAPH_OPERATION_TYPES.items():
            p_method, p_path = pattern.split(" ", 1)
            if p_method != method_upper:
                continue

            # 通配符模式匹配
            if "*" in p_path:
                # 将通配符转换为正则
                regex_pattern = p_path.replace("*", "[^/]+")
                if p_path.endswith("/"):
                    regex_pattern = regex_pattern + ".*"
                if re.match(f"^{regex_pattern}$", path_lower):
                    return op_type
            elif p_path.endswith("/"):
                # 前缀匹配
                if path_lower.startswith(p_path[:-1]):
                    return op_type

        # 默认为 proxy
        return "proxy"

    def _filter_response_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """过滤响应头（移除 hop-by-hop 头）"""
        hop_by_hop = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        }

        return {k: v for k, v in headers.items() if k.lower() not in hop_by_hop}

    async def health_check(self, service_name: str) -> Tuple[bool, str]:
        """
        服务健康检查

        Args:
            service_name: 服务名称

        Returns:
            (健康状态, 消息)
        """
        config = await self.config_loader.get_config(service_name)
        if not config:
            return False, f"Service not found: {service_name}"

        if not config.enabled:
            return False, f"Service disabled: {service_name}"

        try:
            upstream_base = self._select_upstream(config)
            client = await self._get_client(config)

            base = upstream_base.rstrip("/")
            probes: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
                ("GET", f"{base}/health", None),
                # LangGraph agent server commonly exposes /docs and assistants APIs.
                ("GET", f"{base}/docs", None),
                ("POST", f"{base}/assistants/search", {}),
            ]

            last_status: Optional[int] = None
            for method, url, payload in probes:
                if method == "POST":
                    response = await client.post(url, json=payload, timeout=5.0)
                else:
                    response = await client.get(url, timeout=5.0)
                last_status = response.status_code
                if response.status_code < 400:
                    return True, "OK"

            return False, f"Health check failed: {last_status}"

        except Exception as e:
            return False, f"Health check error: {e}"
