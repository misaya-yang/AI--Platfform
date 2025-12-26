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
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from .billing_interceptor import BillingInterceptor, StreamProcessor
from .config_loader import ProxyConfigLoader, ProxyServiceConfig
from .context_injector import ContextInjector, RequestContext
from ..core.observability.logging import get_logger

logger = get_logger(__name__)


# LangGraph 需要 assistant_id 的路径模式
LANGGRAPH_ASSISTANT_PATHS = [
    "/runs",           # POST /runs
    "/runs/stream",    # POST /runs/stream
    "/runs/wait",      # POST /runs/wait
    "/threads/",       # POST /threads/{thread_id}/runs etc.
]


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
        start_time = time.time()
        
        # 1. 获取服务配置
        config = await self.config_loader.get_config(request.service_name)
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
        
        # 5. 获取 HTTP 客户端
        client = await self._get_client(config)
        
        # 6. LangGraph assistant_id 自动注入
        body = request.body
        if config.assistant_id and request.method in ("POST", "PUT", "PATCH"):
            body = self._inject_assistant_id(body, request.path, config.assistant_id)
        
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
                )
            else:
                return await self._proxy_normal(
                    client=client,
                    method=request.method,
                    url=upstream_url,
                    headers=headers,
                    body=body,
                    params=request.query_params,
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
        path_lower = path.lower()
        needs_injection = any(
            pattern in path_lower for pattern in LANGGRAPH_ASSISTANT_PATHS
        )
        
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
    
    async def _proxy_normal(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        params: Dict[str, Any],
    ) -> ProxyResponse:
        """执行普通（非流式）代理请求"""
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
            params=params,
        )
        
        # 提取响应头（过滤 hop-by-hop 头）
        response_headers = self._filter_response_headers(dict(response.headers))
        
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
    ) -> ProxyResponse:
        """
        执行流式代理请求
        
        支持：
        - 自动检测响应 Content-Type，如果不是 text/event-stream 则回退到普通响应
        - 错误透传：原样返回上游 4xx/5xx 错误
        - 计费拦截
        """
        
        # 创建流处理器（用于计费）
        stream_processor: Optional[StreamProcessor] = None
        if self.billing_interceptor:
            stream_processor = self.billing_interceptor.create_stream_processor(
                request_id=context.request_id,
                service_id=config.service_id,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                assistant_id=config.assistant_id or "",
            )
        
        # 先获取响应头，判断是否真的是流式响应
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
                
                # 完成处理
                if stream_processor:
                    await stream_processor.finalize()
                        
            except Exception as e:
                logger.error(f"[Proxy] Streaming error: {e}")
                # 发送错误事件
                error_event = f"event: error\ndata: {str(e)}\n\n"
                yield error_event.encode("utf-8")
            finally:
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
        
        path_lower = path.lower()
        
        # 精确匹配流式后缀
        for suffix in streaming_suffixes:
            if path_lower.endswith(suffix):
                return True
        
        # 检查路径中是否包含 "stream"（但要排除 "upstream" 等）
        if "/stream" in path_lower:
            return True
        
        return False
    
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
        
        return {
            k: v for k, v in headers.items()
            if k.lower() not in hop_by_hop
        }
    
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
            
            # 尝试访问健康检查端点
            health_url = upstream_base.rstrip("/") + "/health"
            response = await client.get(health_url, timeout=5.0)
            
            if response.status_code < 400:
                return True, "OK"
            else:
                return False, f"Health check failed: {response.status_code}"
                
        except Exception as e:
            return False, f"Health check error: {e}"

