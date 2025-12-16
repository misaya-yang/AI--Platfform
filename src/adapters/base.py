from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from ..connectors.base import create_connector
from ..models.request import UnifiedRequest
from ..models.response import StreamChunk, UnifiedResponse
from ..models.service import ServiceDefinition


class ProtocolAdapter(ABC):
    def __init__(self, service: ServiceDefinition):
        self.service = service
        self.connector = create_connector(service)

    @abstractmethod
    async def invoke(self, request: UnifiedRequest) -> UnifiedResponse:
        raise NotImplementedError

    async def stream(self, request: UnifiedRequest) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError

    async def submit(self, request: UnifiedRequest) -> str:
        raise NotImplementedError

    async def get_result(self, task_id: str) -> UnifiedResponse:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return await self.connector.health_check()

    def transform_request(self, request: UnifiedRequest) -> Any:
        return request

    def transform_response(self, response: Any) -> UnifiedResponse:
        return response
