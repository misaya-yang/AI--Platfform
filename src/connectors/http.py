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
        response = await self._client.request(method, url, **kwargs)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response

    async def health_check(self) -> bool:
        endpoint = (self.service.connector_config or {}).get("health_endpoint", "/health")
        try:
            response = await self._client.get(endpoint)
            return response.status_code < 500
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
