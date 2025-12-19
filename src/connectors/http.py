from __future__ import annotations

from typing import Any

import httpx

from .base import BaseConnector
from ..models.service import ServiceDefinition


class HTTPConnector(BaseConnector):
    def __init__(self, service: ServiceDefinition):
        super().__init__(service)
        config = service.connector_config or {}
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        
        # 超时配置
        default_timeout = config.get("timeout", service.timeout) or 60.0
        stream_timeout = config.get("stream_timeout", 300.0)  # 流式响应默认 5 分钟
        connect_timeout = config.get("connect_timeout", 10.0)
        
        # 使用分开的超时配置，流式响应需要更长的读取超时
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=stream_timeout,
            write=default_timeout,
            pool=default_timeout,
        )
        
        headers = config.get("headers") or {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        # 支持动态传递额外的 headers（与 client 默认 headers 合并）
        extra_headers = kwargs.pop("headers", None)
        if extra_headers:
            merged_headers = dict(self._client.headers)
            merged_headers.update(extra_headers)
            kwargs["headers"] = merged_headers
        
        response = await self._client.request(method, url, **kwargs)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response

    async def health_check(self, headers: dict = None) -> bool:
        """
        健康检查。
        
        Args:
            headers: 可选的认证头部，用于需要认证的服务
        """
        config = self.service.connector_config or {}
        endpoint = config.get("health_endpoint", "/health")
        # 对于需要认证的服务，可以配置 health_skip_auth 跳过健康检查的认证
        skip_auth = config.get("health_skip_auth", False)
        
        try:
            request_headers = None
            if headers and not skip_auth:
                # 合并客户端默认头部和传入的认证头部
                request_headers = dict(self._client.headers)
                request_headers.update(headers)
            
            response = await self._client.get(endpoint, headers=request_headers)
            # 403 表示认证问题，但服务本身是健康的
            return response.status_code < 500
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
