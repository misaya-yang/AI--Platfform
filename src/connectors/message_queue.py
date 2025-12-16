from __future__ import annotations

from typing import Any

from .base import BaseConnector
from ..models.service import ServiceDefinition


class MessageQueueConnector(BaseConnector):
    def __init__(self, service: ServiceDefinition):
        super().__init__(service)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError
