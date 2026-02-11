from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Any

from ..models.enums import ConnectorType
from ..models.service import ServiceDefinition


class BaseConnector(ABC):
    def __init__(self, service: ServiceDefinition):
        self.service = service

    @abstractmethod
    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def health_check(self, headers: dict = None) -> bool:
        """
        健康检查。

        Args:
            headers: 可选的认证头部，用于需要认证的服务
        """
        return True

    async def close(self) -> None:
        return None


class InProcessConnector(BaseConnector):
    def __init__(self, service: ServiceDefinition):
        super().__init__(service)
        config = service.connector_config or {}
        self.module_path = config.get("module") or config.get("graph_module")
        self.object_name = config.get("callable") or config.get("graph_name")
        self._callable: Any | None = None
        if self.module_path and self.object_name:
            module = importlib.import_module(self.module_path)
            self._callable = getattr(module, self.object_name, None)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if not self._callable:
            raise RuntimeError("No in-process callable configured")
        payload = kwargs.get("json") or kwargs.get("data") or kwargs
        result = self._callable(payload)
        if hasattr(result, "__await__"):
            result = await result
        return result


def create_connector(service: ServiceDefinition) -> BaseConnector:
    connector_type = service.connector_type
    if connector_type == ConnectorType.HTTP:
        from .http import HTTPConnector

        return HTTPConnector(service)
    if connector_type == ConnectorType.WEBSOCKET:
        from .websocket import WebSocketConnector

        return WebSocketConnector(service)
    if connector_type == ConnectorType.GRPC:
        from .grpc import GRPCConnector

        return GRPCConnector(service)
    if connector_type == ConnectorType.MESSAGE_QUEUE:
        from .message_queue import MessageQueueConnector

        return MessageQueueConnector(service)
    if connector_type == ConnectorType.IN_PROCESS:
        return InProcessConnector(service)
    raise ValueError(f"Unsupported connector type: {connector_type}")
